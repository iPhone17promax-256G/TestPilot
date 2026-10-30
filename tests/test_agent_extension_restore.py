"""MCP 工具目录改变后，真实 HITL 存档仍需完整消费原审批批次。"""
import json
from types import SimpleNamespace

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import Field
from sqlalchemy import select

from testpilot import testing as t
from testpilot.agent import permissions, runtime
from testpilot.storage import Record, read_record, session, update_record
from testpilot.tools import DEFINITIONS
from testpilot.workspace import project_scope


class ExtensionRestoreModel(BaseChatModel):
    calls: list[dict] = Field(default_factory=list)
    histories: list[list] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "extension-restore-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.histories.append(list(messages))
        answered = any(isinstance(message, ToolMessage) for message in messages)
        reply = AIMessage(content="已读取两项操作结果" if answered else "准备两项操作",
                          tool_calls=[] if answered else self.calls)
        return ChatResult(generations=[ChatGeneration(message=reply)])


class AskReviewer:
    def invoke(self, messages, **kwargs):
        return AIMessage(content=json.dumps(dict(decision="ask", reason="这次操作需用户确认"), ensure_ascii=False))


@pytest.fixture
def task():
    project = t.project_save("MCP 审批恢复项目")
    with project_scope(project["id"]):
        environment = t.environment_save("测试目标", "http://target.test")
        conversation = t.task_start("调用 MCP，并保存工作区文件，先确认", environment["id"])
        update_record(conversation["id"], lambda payload: payload.update(status="running"))
        yield conversation["id"]
    with session() as db:
        for row in db.scalars(select(Record).where((Record.project_id == project["id"]) | (Record.id == project["id"]))):
            db.delete(row)


@pytest.mark.parametrize("removed_decision", ["approve", "reject"])
def test_removed_mcp_tool_does_not_change_original_hitl_batch(monkeypatch, task, removed_decision):
    # 第一次模型回复提出两个 ask。没有决定前，保留项和之后撤下的 MCP 项都不能执行。
    calls = [
        dict(name="mcp_demo", args=dict(path="external.txt", content="external"), id="mcp-disappeared", type="tool_call"),
        dict(name="workspace_patch", args=dict(path="retained.txt", content="retained"), id="local-retained", type="tool_call"),
    ]
    model = ExtensionRestoreModel(calls=calls)
    reviewer = AskReviewer()
    monkeypatch.setattr(runtime, "ChatOpenAI", lambda **kwargs: reviewer if kwargs["max_tokens"] == 1000 else model)
    external_effects = []

    def external_operation(**arguments):
        external_effects.append(arguments)
        return dict(unexpectedExecution=True)

    external_tool = StructuredTool(
        name="mcp_demo", description="外部 MCP 示例工具",
        args_schema=DEFINITIONS["workspace_patch"][0], func=external_operation)
    initial_extensions = SimpleNamespace(scrub=lambda value: value, tools=lambda: [external_tool], rows=[], statuses={})
    runtime._execute_task(task, initial_extensions, [])
    paused = read_record(task)
    assert paused["status"] == "waiting_approval"
    assert external_effects == []
    retained = t.safe_path(f"workspaces/{task}/retained.txt")
    assert not retained.exists()
    entries = {entry["callId"]: entry for entry in paused["approvals"]}
    assert len(entries) == 2

    # 下一轮连接失败或配置已停用时，该 MCP 不再注册为可执行工具。
    # 即使旧卡片已获批准，也不能执行撤下项；原存档仍有两项决定，不能缩成一项来恢复。
    permissions.decide(task, entries["mcp-disappeared"]["id"], removed_decision)
    permissions.decide(task, entries["local-retained"]["id"], "approve")
    update_record(task, lambda payload: payload.update(status="running"))
    restored_extensions = SimpleNamespace(scrub=lambda value: value, tools=lambda: [], rows=[], statuses={})
    runtime._execute_task(task, restored_extensions, [])

    result = read_record(task)
    assert result["status"] == "completed", result
    assert retained.read_text() == "retained"
    assert external_effects == []
    restored = {entry["callId"]: entry for entry in result["approvals"]}
    assert restored["mcp-disappeared"]["status"] == ("stale" if removed_decision == "approve" else "rejected")
    assert restored["local-retained"]["status"] == "executed"
    events = {event["callId"]: event for event in result["events"]}
    assert events["mcp-disappeared"]["status"] == "blocked"
    assert events["local-retained"]["status"] == "completed"
    replies = {message.tool_call_id: message for message in model.histories[-1] if isinstance(message, ToolMessage)}
    assert set(replies) == {"mcp-disappeared", "local-retained"}
    assert replies["mcp-disappeared"].status == "error"
    assert replies["local-retained"].status == "success"
