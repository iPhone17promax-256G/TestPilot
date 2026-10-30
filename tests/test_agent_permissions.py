"""Real checkpoint pause/resume against the disposable MySQL test database."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from sqlalchemy import select

from testpilot import testing as t
from testpilot.agent import permissions as p, runtime
from testpilot.api import app
from testpilot.config import settings
from testpilot.storage import Record, read_record, session, uid, update_record
from testpilot.tools import dispatch
from testpilot.job_queue import RedisJobs
from queue_helpers import mark_running
from testpilot.workspace import project_scope


class FakeModel(BaseChatModel):
    calls: list[dict] = Field(default_factory=list)
    histories: list[list] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "permission-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        # 保留每次主模型实际收到的完整消息序列，检查审批恢复/续聊有没有漏消息、重复消息或配错工具结果。
        self.histories.append(list(messages))
        answered = any(isinstance(m, ToolMessage) for m in messages)
        message = AIMessage(content="已按审批结果处理" if answered else "准备操作", tool_calls=[] if answered else self.calls)
        return ChatResult(generations=[ChatGeneration(message=message)])


class Reviewer:
    def __init__(self, decision="ask", reason="用户要求这次先确认"):
        self.decision, self.reason, self.calls = decision, reason, 0

    def invoke(self, messages, **kwargs):
        self.calls += 1
        return AIMessage(content=json.dumps(dict(decision=self.decision, reason=self.reason), ensure_ascii=False),
                         usage_metadata=dict(input_tokens=12, output_tokens=8, total_tokens=20))


@pytest.fixture
def task():
    project = t.project_save("权限检查项目")
    with project_scope(project["id"]):
        env = t.environment_save("测试目标", "http://target.test")
        task = t.task_start("在工作区保存说明，先让我确认", env["id"])
        update_record(task["id"], lambda v: v.update(status="running"))
        yield task["id"]
    with session() as db:
        for row in db.scalars(select(Record).where((Record.project_id == project["id"]) | (Record.id == project["id"]))):
            db.delete(row)


def call(name="workspace_patch", arguments=None, identity="call-write"):
    return dict(name=name, args=arguments if arguments is not None else dict(path="note.txt", content="TestPilot approval test"), id=identity, type="tool_call")


def extension():
    return SimpleNamespace(scrub=lambda value: value, tools=lambda: [], rows=[], statuses={})


def install_model(monkeypatch, calls, reviewer):
    model = FakeModel(calls=calls)
    monkeypatch.setattr(runtime, "ChatOpenAI", lambda **kwargs: reviewer if kwargs["max_tokens"] == 1000 else model)
    return model


def run(task):
    update_record(task, lambda v: v.update(status="running"))
    runtime._execute_task(task, extension(), [])


def test_real_mysql_pause_approve_resume_and_no_duplicate(monkeypatch, task):
    reviewer = Reviewer()
    install_model(monkeypatch, [call()], reviewer)
    runtime._execute_task(task, extension(), [])
    paused = read_record(task)
    assert paused["status"] == "waiting_approval"
    assert not t.safe_path(f"workspaces/{task}/note.txt").exists()
    entry = paused["approvals"][0]
    assert entry["interruptId"] and entry["decision"] == "ask"
    p.decide(task, entry["id"], "approve")
    p.decide(task, entry["id"], "approve")  # 模拟用户对同一审批重复点击相同决定，检查重复提交复用已有结果，不额外创建一次批准或作业。
    # 重新创建 Agent 运行对象和 checkpoint 连接，模拟上一执行子进程已退出、下一子进程读取存档续跑；不是复用旧对象的内存状态。
    run(task)
    result = read_record(task)
    assert result["status"] == "completed", result
    assert result["approvals"][0]["status"] == "executed"
    assert t.safe_path(f"workspaces/{task}/note.txt").read_text() == "TestPilot approval test"
    assert len(result["events"]) == 1
    assert json.loads(result["events"][0]["result"])["_permission"]["approvedBy"] == "user"
    assert reviewer.calls == 1
    assert result["approvalUsage"]["totalTokens"] == 20
    assert not p.check(task, call())[0]  # 再次恢复同一个工具调用，核对副作用计数不增加，证明使用已记录的调用结果而未重新执行该操作。


@pytest.mark.parametrize("resolution", ["reject", "message", "readonly", "cancel"])
def test_reject_steer_mode_change_and_cancel_do_not_execute(monkeypatch, task, resolution):
    reviewer = Reviewer()
    install_model(monkeypatch, [call()], reviewer)
    runtime._execute_task(task, extension(), [])
    entry = read_record(task)["approvals"][0]
    if resolution == "reject":
        p.decide(task, entry["id"], "reject", "只解释，不保存")
    elif resolution == "message":
        t.task_message(task, "不要保存了，解释一下")
    elif resolution == "readonly":
        p.set_mode(task, "readonly")
    else:
        t.task_cancel(task)
        assert read_record(task)["status"] == "cancelled"
    with pytest.raises(p.ApprovalConflict):
        p.decide(task, entry["id"], "approve")
    if resolution != "cancel":
        run(task)
        result = read_record(task)
        assert result["status"] == "completed", result
        assert result["events"][0]["status"] == "blocked"
        if resolution == "message":
            assert next(m for m in result["messages"] if m.get("content") == "不要保存了，解释一下")["delivered"]
    assert not t.safe_path(f"workspaces/{task}/note.txt").exists()


def test_batch_waits_for_all_decisions(monkeypatch, task):
    reviewer = Reviewer()
    calls = [call(identity="call-one"), call(arguments=dict(path="other.txt", content="other"), identity="call-two")]
    install_model(monkeypatch, calls, reviewer)
    runtime._execute_task(task, extension(), [])
    entries = read_record(task)["approvals"]
    assert len(entries) == 2 and entries[0]["interruptId"] == entries[1]["interruptId"]
    p.decide(task, entries[0]["id"], "approve")
    assert read_record(task)["status"] == "waiting_approval"
    p.decide(task, entries[1]["id"], "reject")
    run(task)
    assert t.safe_path(f"workspaces/{task}/note.txt").is_file()
    assert not t.safe_path(f"workspaces/{task}/other.txt").exists()


@pytest.mark.parametrize("mode,decision,executed,calls", [("auto", "allow", True, 1), ("auto", "deny", False, 1), ("bypass", "ask", True, 0), ("readonly", "allow", False, 0)])
def test_modes_enforced_at_actual_tool(monkeypatch, task, mode, decision, executed, calls):
    p.set_mode(task, mode)
    reviewer = Reviewer(decision)
    install_model(monkeypatch, [call()], reviewer)
    run(task)
    assert t.safe_path(f"workspaces/{task}/note.txt").exists() == executed
    assert reviewer.calls == calls
    assert read_record(task)["status"] == "completed"


def test_readonly_reads_and_forged_mutation_or_mcp_cannot_bypass(task):
    from testpilot.mcp_client import MCPHub
    p.set_mode(task, "readonly")
    assert dispatch("cases_list", {}, task_id=task) == []
    for name, args in [("workspace_patch", dict(path="note.txt", content="forged")), ("runs_start", dict(case_id="any", revision_number=1))]:
        with pytest.raises(PermissionError):
            dispatch(name, args, task_id=task)
    with pytest.raises(PermissionError):
        MCPHub([], owner=task).call("mcp_fake", {})


def test_bound_arguments_scope_and_failure_default(task):
    t.take_user_messages(task)
    reviewer = Reviewer("allow")
    entry = p.review(task, call(), reviewer, "write own file", lambda s: s)
    assert entry["status"] == "allowed"
    assert not p.check(task, call(arguments=dict(path="other.txt", content="TestPilot approval test")))[0]
    env = read_record(task)["environmentId"]
    update_record(env, lambda v: v.update(url="http://changed.test"))
    assert not p.check(task, call())[0]
    bad = SimpleNamespace(invoke=lambda _, **kw: AIMessage(content="invalid json"))
    verdict, _ = p.judge(bad, read_record(task), "workspace_patch", {}, "test", lambda s: s)
    assert verdict.decision == "ask"
    bad.invoke = lambda _, **kw: (_ for _ in ()).throw(RuntimeError("provider failed"))
    assert p.judge(bad, read_record(task), "workspace_patch", {}, "test", lambda s: s)[0].decision == "ask"


def test_approval_api_project_boundary_and_conflict(monkeypatch, task):
    install_model(monkeypatch, [call()], Reviewer())
    runtime._execute_task(task, extension(), [])
    entry = read_record(task)["approvals"][0]
    client = TestClient(app, base_url="https://testserver", headers={"Authorization": "Bearer " + settings.access_token, "X-TestPilot-Project": read_record(task)["projectId"]})
    assert client.post(f"/api/tasks/{task}/approvals/{entry['id']}", json={"decision": "reject"}).status_code == 202
    assert client.post(f"/api/tasks/{task}/approvals/{entry['id']}", json={"decision": "approve"}).status_code == 409
    assert client.post(f"/api/tasks/{task}/permissions", headers={"X-TestPilot-Project": "PROJECT"}, json={"mode": "bypass"}).status_code == 404


def test_auxiliary_approval_binds_script_contents_and_replays_evidence(task):
    t.take_user_messages(task)
    path = t.safe_path(f"workspaces/{task}/analyze.py")
    path.parent.mkdir(parents=True)
    path.write_text("print('first')")
    operation = call("workspace_exec", dict(argv=["python", "analyze.py"], timeout=30))
    p.review(task, operation, Reviewer("allow"), "isolated command", lambda s: s)
    assert p.check(task, operation)[0]
    path.write_text("print('changed after approval')")
    assert not p.check(task, operation)[0]
    hashes, scripts = p.workspace_evidence(read_record(task), operation["args"])
    assert len(hashes) == 1 and "changed after approval" in scripts["analyze.py"]
    t.task_event(task, operation["name"], "{}", '{"exitCode":0}', operation["id"])
    p.finished(task, operation["id"], "success")
    assert json.loads(p.replayed(task, operation["id"])["content"])["exitCode"] == 0


def test_approval_queued_only_after_all_resolved_and_read_approval_survives_reload(monkeypatch, task):
    reviewer = Reviewer()
    install_model(monkeypatch, [call("cases_list", {})], reviewer)
    # 模拟 Agent Job 已被执行器领取但尚未完成收尾：用户的审批决定可能在旧子进程退出前到达，检查这种先后顺序仍能正确重新排队。
    leased=mark_running(task)
    runtime._execute_task(task, extension(), [])
    paused = read_record(task)
    assert paused["status"] == "waiting_approval"
    client = TestClient(app, base_url="https://testserver", headers={"Authorization": "Bearer " + settings.access_token, "X-TestPilot-Project": paused["projectId"]})
    snapshot = client.get("/api/state").json()
    reloaded = next(v for v in snapshot["tasks"] if v["id"] == task)
    assert reloaded["approvals"][0]["interruptId"] == paused["approvals"][0]["interruptId"]
    p.decide(task, paused["approvals"][0]["id"], "approve")
    job=RedisJobs().for_record(task)
    assert job.status == "running" and job.claim == leased.claim
    assert read_record(task)["resumeRequested"]
    run(task)
    assert read_record(task)["status"] == "completed" and not read_record(task)["resumeRequested"]


def test_judge_reads_actual_run_revision_and_approval_limits_still_hold(task):
    t.take_user_messages(task)
    item = t.case_save("健康检查", "api", "", steps=[{"action": "request", "path": "/ping", "expected_status": 200}])
    seen = []
    def inspect(messages, **kwargs):
        seen.append(json.loads(messages[-1]["content"]))
        return AIMessage(content='{"decision":"allow","reason":"用户授权这条健康检查"}')
    operation = call("runs_start", dict(case_id=item["id"], revision_number=1), "call-run")
    entry = p.review(task, operation, SimpleNamespace(invoke=inspect), "run a fixed version", lambda s: s)
    assert seen[0]["testRevision"]["steps"][0]["path"] == "/ping"
    assert entry["status"] == "allowed"
    p.set_mode(task, "bypass")
    escape = call(arguments=dict(path="../../escape.txt", content="forged"), identity="escape")
    p.review(task, escape, None, "write file", lambda s: s)
    with p.execution_permit(task, escape), pytest.raises(ValueError, match="相对路径"):
        dispatch(escape["name"], escape["args"], task_id=task)


# 功能：让同名工具的不同参数得到不同审批决定，暴露按名字匹配 HITL 决定而配错调用的问题。
# 输入：decisions 是工作区文件名到 allow/ask/deny 的映射；invoke 接收真实审批层组织的模型消息。
# 输出/影响：返回与普通 Reviewer 相同的 JSON 和用量，不运行工具；calls 记录审查次数，便于检查恢复没有重复审查。
class PathReviewer(Reviewer):
    def __init__(self, decisions):
        super().__init__()
        self.decisions = decisions

    def invoke(self, messages, **kwargs):
        self.calls += 1
        context = json.loads(messages[-1]["content"])
        decision = self.decisions[context["arguments"]["path"]]
        return AIMessage(content=json.dumps(dict(decision=decision, reason="按本次文件参数独立判断"), ensure_ascii=False),
                         usage_metadata=dict(input_tokens=12, output_tokens=8, total_tokens=20))


def test_hitl_mixed_batch_has_no_early_effect_and_audits_denial(monkeypatch, task):
    # 同一条模型消息同时提出自动允许、人工确认和拒绝的三个写文件调用，三个名字相同、参数不同。
    # 整批在人工决定到来前都应暂停；允许项也不能抢先产生副作用。
    files = {"call-allowed": "allowed.txt", "call-asked": "asked.txt", "call-denied": "denied.txt"}
    calls = [call(arguments=dict(path=path, content=identity), identity=identity) for identity, path in files.items()]
    reviewer = PathReviewer({"allowed.txt": "allow", "asked.txt": "ask", "denied.txt": "deny"})
    model = install_model(monkeypatch, calls, reviewer)
    runtime._execute_task(task, extension(), [])
    paused = read_record(task)
    assert paused["status"] == "waiting_approval"
    assert all(not t.safe_path(f"workspaces/{task}/{path}").exists() for path in files.values())
    approvals = {entry["callId"]: entry for entry in paused["approvals"]}
    assert {identity: entry["status"] for identity, entry in approvals.items()} == {
        "call-allowed": "allowed", "call-asked": "pending", "call-denied": "denied"}

    # 下一次 _execute_task 会创建全新的 Agent 图对象和存档连接；只能执行原 allow 项与本次批准项。
    p.decide(task, approvals["call-asked"]["id"], "approve")
    run(task)
    result = read_record(task)
    assert result["status"] == "completed", result
    assert t.safe_path(f"workspaces/{task}/allowed.txt").read_text() == "call-allowed"
    assert t.safe_path(f"workspaces/{task}/asked.txt").read_text() == "call-asked"
    assert not t.safe_path(f"workspaces/{task}/denied.txt").exists()
    events = {event["callId"]: event for event in result["events"]}
    assert {identity: event["status"] for identity, event in events.items()} == {
        "call-allowed": "completed", "call-asked": "completed", "call-denied": "blocked"}
    assert json.loads(events["call-allowed"]["result"])["_permission"]["approvedBy"] == "ai"
    assert json.loads(events["call-asked"]["result"])["_permission"]["approvalId"] == approvals["call-asked"]["id"]
    replies = {message.tool_call_id: message for message in model.histories[-1] if isinstance(message, ToolMessage)}
    assert set(replies) == set(files)
    assert replies["call-denied"].status == "error"
    assert reviewer.calls == 3


def test_hitl_same_tool_uses_approval_identity_when_decided_in_reverse_order(monkeypatch, task):
    # 两次调用的工具名相同；用户先批准第二项、再拒绝第一项。
    # 审批卡片顺序、点击顺序和工具名都不能替代业务审批 ID / callId 的精确关联。
    model = install_model(monkeypatch, [
        call(arguments=dict(path="first.txt", content="first"), identity="call-first"),
        call(arguments=dict(path="second.txt", content="second"), identity="call-second")], Reviewer())
    runtime._execute_task(task, extension(), [])
    approvals = {entry["callId"]: entry for entry in read_record(task)["approvals"]}
    p.decide(task, approvals["call-second"]["id"], "approve")
    assert read_record(task)["status"] == "waiting_approval"
    p.decide(task, approvals["call-first"]["id"], "reject")
    run(task)
    result = read_record(task)
    assert result["status"] == "completed", result
    assert not t.safe_path(f"workspaces/{task}/first.txt").exists()
    assert t.safe_path(f"workspaces/{task}/second.txt").read_text() == "second"
    events = {event["callId"]: event for event in result["events"]}
    assert events["call-first"]["status"] == "blocked"
    assert json.loads(events["call-second"]["result"])["_permission"]["approvalId"] == approvals["call-second"]["id"]
    replies = {message.tool_call_id: message for message in model.histories[-1] if isinstance(message, ToolMessage)}
    assert replies["call-first"].status == "error"
    assert replies["call-second"].status == "success"


@pytest.mark.parametrize("changed", ["environment", "script"])
def test_approved_checkpoint_rechecks_changed_input_before_handler(monkeypatch, task, changed):
    # 批准时输入有效，但用户批准后、下一个工作进程恢复前，目标环境或待执行脚本已经发生变化。
    # 在真实工具包装入口核对旧摘要，必须阻断，而不是只在审批 API 点击时检查一次。
    if changed == "script":
        path = t.safe_path(f"workspaces/{task}/analyze.py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("print('approved version')")
        operation = call("workspace_exec", dict(argv=["python", "analyze.py"], timeout=30), "call-stale")
    else:
        operation = call(arguments=dict(path="stale.txt", content="must not run"), identity="call-stale")
    invoked = []
    original_dispatch = runtime.dispatch

    # 功能：记录恢复后是否真的进入了该操作的业务 handler；辅助命令若误放行，返回假结果而不启动 Docker。
    # 输入：name/arguments/task_id/call_id 与 runtime 注册的 operation 调用一致。
    # 输出/影响：合法的其他工具仍交真实 dispatch；本例只用 invoked 判断旧调用有没有越过授权边界。
    def observe_dispatch(name, arguments, **kwargs):
        invoked.append(name)
        if name == "workspace_exec":
            return dict(exitCode=0, output="unexpected execution")
        return original_dispatch(name, arguments, **kwargs)

    monkeypatch.setattr(runtime, "dispatch", observe_dispatch)
    install_model(monkeypatch, [operation], Reviewer())
    runtime._execute_task(task, extension(), [])
    entry = read_record(task)["approvals"][0]
    p.decide(task, entry["id"], "approve")
    if changed == "script":
        path.write_text("print('changed after approval')")
    else:
        update_record(read_record(task)["environmentId"], lambda value: value.update(url="http://new-target.test"))
    run(task)
    result = read_record(task)
    assert result["status"] == "completed", result
    assert invoked == []
    event = next(event for event in result["events"] if event["callId"] == "call-stale")
    assert event["status"] == "blocked"
    assert not t.safe_path(f"workspaces/{task}/stale.txt").exists()


def test_message_during_hitl_pause_reaches_resumed_model_exactly_once(monkeypatch, task):
    model = install_model(monkeypatch, [call()], Reviewer())
    runtime._execute_task(task, extension(), [])
    old_approval = read_record(task)["approvals"][0]
    followup = "不要写文件了，只解释这次审批为什么暂停"
    t.task_message(task, followup)
    message = next(message for message in read_record(task)["messages"] if message.get("content") == followup)
    with pytest.raises(p.ApprovalConflict):
        p.decide(task, old_approval["id"], "approve")

    # 新要求使旧调用失效；恢复仍需补齐原调用的 ToolMessage，然后把新用户消息交给模型。
    # delivered=True 只说明业务消息被领取；同时检查模型历史，才能发现图里漏送或重复追加的问题。
    run(task)
    result = read_record(task)
    assert result["status"] == "completed", result
    assert not t.safe_path(f"workspaces/{task}/note.txt").exists()
    assert next(event for event in result["events"] if event["callId"] == "call-write")["status"] == "blocked"
    received = [item for item in model.histories[-1] if isinstance(item, HumanMessage) and item.id == message["id"]]
    assert len(received) == 1 and followup in received[0].content
    assert next(item for item in result["messages"] if item["id"] == message["id"])["delivered"]

    # 再次重建运行对象也沿用同一段图历史，不把已经交付的这条用户消息重新加入。
    run(task)
    assert sum(isinstance(item, HumanMessage) and item.id == message["id"] for item in model.histories[-1]) == 1


def test_legacy_approval_ids_checkpoint_resumes_in_new_hitl_agent(monkeypatch, task):
    from langchain.agents import create_agent
    from langchain.agents.middleware import after_model
    from langchain_core.tools import StructuredTool
    from langgraph.types import interrupt
    from testpilot.agent.conversation import ConversationSession
    from testpilot.tools import DEFINITIONS

    # 这里故意构造升级前的真实暂停图：旧 middleware 名和 approvalIds 载荷必须保留。
    # 只在兼容性测试里使用底层 interrupt；产品代码使用官方 HumanInTheLoopMiddleware。
    operation = call(arguments=dict(path="legacy.txt", content="legacy approved"), identity="legacy-write")
    reviewer = Reviewer()
    original_messages = t.take_user_messages(task)

    # 功能：复现旧 permission_review.after_model 的存档协议，让新实现读取真实旧 checkpoint。
    # 输入：state 含旧主模型提出的 tool_calls；审批仍通过现有业务层保存到 Task.approvals。
    # 输出/影响：通过 approvalIds 中断并写入 saver；该步骤不调用工具、不产生写文件副作用。
    @after_model
    def permission_review(state, runtime):
        entries = [p.review(task, item, reviewer, DEFINITIONS[item["name"]][1], lambda value: value)
                   for item in state["messages"][-1].tool_calls]
        interrupt({"approvalIds": [entry["id"] for entry in entries if entry["decision"] == "ask"]})

    # 功能：旧图只负责留下暂停进度；若在批准前越过暂停点运行了工具，立即让测试失败。
    # 输入：arguments 是旧图工具参数；输出/影响：总是抛 AssertionError，不触碰工作区。
    def must_not_execute_before_upgrade(**arguments):
        raise AssertionError("旧图只能保存暂停点，不能提前运行工具")

    with ConversationSession(task, int(t.model_settings()["maxSteps"])) as conversation:
        conversation.checkpointer.setup()
        legacy = create_agent(model=FakeModel(calls=[operation]), tools=[StructuredTool(
            name="workspace_patch", description=DEFINITIONS["workspace_patch"][1],
            args_schema=DEFINITIONS["workspace_patch"][0], func=must_not_execute_before_upgrade)],
            middleware=[permission_review], checkpointer=conversation.checkpointer)
        config = {"configurable": {"thread_id": task}, "recursion_limit": 50}
        legacy.invoke({"messages": [HumanMessage(content=message["content"], id=message["id"])
                                    for message in original_messages]}, config=config)
        snapshot = legacy.get_state(config)
        # 实际 LangGraph 版本可能同时在 snapshot.interrupts 和 snapshot.tasks 上暴露暂停项，按 ID 去重。
        interrupts = {item.id: item for item in getattr(snapshot, "interrupts", ())}
        for pending in snapshot.tasks:
            interrupts.update({item.id: item for item in pending.interrupts})
        assert interrupts and all(set(item.value) == {"approvalIds"} for item in interrupts.values())
        p.paused(task, list(interrupts.values()))

    # 关闭旧图对象及存档连接后批准，再由新版 runtime 重新创建图和连接恢复。
    # 原批准可以继续使用，但仍经过最新摘要核验；恢复不能重发旧模型决策或再次运行已经完成的副作用。
    entry = read_record(task)["approvals"][0]
    assert read_record(task)["status"] == "waiting_approval"
    assert not t.safe_path(f"workspaces/{task}/legacy.txt").exists()
    p.decide(task, entry["id"], "approve")
    model = install_model(monkeypatch, [operation], reviewer)
    run(task)
    result = read_record(task)
    assert result["status"] == "completed", result
    assert t.safe_path(f"workspaces/{task}/legacy.txt").read_text() == "legacy approved"
    assert result["approvals"][0]["status"] == "executed"
    assert len(result["events"]) == 1 and result["events"][0]["callId"] == "legacy-write"
    assert json.loads(result["events"][0]["result"])["_permission"]["approvedBy"] == "user"
    assert reviewer.calls == 1
    assert any(isinstance(message, ToolMessage) and message.tool_call_id == "legacy-write"
               for message in model.histories[-1])


def test_executor_sqlite_checkpoint_survives_rebuild_and_is_account_scoped(monkeypatch, tmp_path, task):
    import sqlite3
    from testpilot.agent.conversation import ConversationSession
    from testpilot.workspace import user_scope

    owner = read_record(task)["ownerId"]
    monkeypatch.setattr(settings, "role", "executor")
    monkeypatch.setattr(settings, "client_root", tmp_path)
    monkeypatch.setattr(settings, "executor_user", "checkpoint-test")
    monkeypatch.setattr(settings, "executor_claim", "")
    monkeypatch.setattr(settings, "data", tmp_path / "data")
    reviewer = Reviewer()
    install_model(monkeypatch, [call()], reviewer)

    # 本例单独检验真实执行端的账号 SQLite 存档；业务记录仍使用隔离 MySQL fixture。
    # 空 claim 不启用副本 HTTP，同步保留原业务账号上下文，避免切存档路径时误变 Task.ownerId 身份。
    with user_scope(owner):
        runtime._execute_task(task, extension(), [])
        paused = read_record(task)
        path = tmp_path / "users" / "checkpoint-test" / "checkpoints.sqlite"
        assert paused["status"] == "waiting_approval"
        assert path.is_file()
        assert not t.safe_path(f"workspaces/{task}/note.txt").exists()
        # 不仅检查文件存在，还核对真实 saver 已经为本 Task 写下恢复点，排除空 SQLite 文件造成的假通过。
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM checkpoints WHERE thread_id = ?", (task,)).fetchone()[0] > 0

        # 旧 Agent 及存档连接已经退出；批准后由新一轮 _execute_task 重新打开同一路径并从工具暂停位置继续。
        p.decide(task, paused["approvals"][0]["id"], "approve")
        run(task)
        result = read_record(task)
        assert result["status"] == "completed", result
        assert result["approvals"][0]["status"] == "executed"
        assert not result.get("checkpointMigrated"), "有完整 SQLite 存档时不应退回文字历史迁移"
        assert t.safe_path(f"workspaces/{task}/note.txt").read_text() == "TestPilot approval test"
        assert reviewer.calls == 1
        assert len(result["events"]) == 1
        assert json.loads(result["events"][0]["result"])["_permission"]["approvedBy"] == "user"

        # 另一个账号使用自己的 SQLite 文件；即使查询相同 Task/thread_id，也不能读取上述恢复点。
        # 这里仅读存档，不运行另一账号的 Agent，也不修改原 Task 的业务归属。
        with monkeypatch.context() as other_account:
            other_account.setattr(settings, "executor_user", "checkpoint-other")
            with ConversationSession(task, int(t.model_settings()["maxSteps"])) as conversation:
                assert conversation.checkpointer.get_tuple({"configurable": {"thread_id": task}}) is None

        # 回到原账号再次重建运行对象；既有工具结果应继续复用，不能多做一次写入或重复审查。
        run(task)
        result = read_record(task)
        assert result["status"] == "completed", result
        assert reviewer.calls == 1 and len(result["events"]) == 1


# 功能：模拟真正逐轮决策的主模型，让后一个工具调用依赖前一个工具已经返回的真实结果。
# 输入：框架送来的 messages 含当前对话历史；文件名和内容由本模型的测试字段指定。
# 输出/影响：依次提出一次写文件、一次读文件、最后返回根据读回内容组织的回答；只模拟模型，不替代真实工具。
class MultiRoundModel(FakeModel):
    filename: str = "multi-round.txt"
    written: str = "TestPilot two independent tool rounds"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.histories.append(list(messages))
        results = {message.tool_call_id: message for message in messages if isinstance(message, ToolMessage)}
        if "loop-write" not in results:
            message = AIMessage(content="先写入说明文件", id="loop-write-answer", tool_calls=[
                call(arguments=dict(path=self.filename, content=self.written), identity="loop-write")])
        elif "loop-read" not in results:
            # 第二次请求必须已经看到第一轮真实 handler 返回的结果；不是第一次批量提出两个调用。
            saved = json.loads(results["loop-write"].content)
            assert results["loop-write"].status == "success" and saved["path"] == self.filename
            message = AIMessage(content="已经写入，现在读回核对", id="loop-read-answer", tool_calls=[
                call("workspace_read", dict(path=self.filename), "loop-read")])
        else:
            actual = json.loads(results["loop-read"].content)
            assert results["loop-read"].status == "success" and actual["content"] == self.written
            message = AIMessage(content="已写入并读回：" + actual["content"], id="loop-final-answer")
        return ChatResult(generations=[ChatGeneration(message=message)])


# 功能：模拟正常续聊中的纯讨论回答，验证新主模型能看到旧工具结果与本轮追问，但无需再次调用工具。
# 输入：expected_question 是本轮用户问题；框架提供恢复后的 messages。
# 输出/影响：返回无 tool_calls 的普通助手正文；记录 histories，不运行工具、不调用审批模型。
class DiscussionModel(FakeModel):
    expected_question: str
    reply: str = "上一轮先写文件，再依据写入结果读回核对；这次仅解释，没有继续执行工具。"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.histories.append(list(messages))
        latest = next(message for message in reversed(messages) if isinstance(message, HumanMessage))
        assert self.expected_question in latest.content
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply, id="discussion-answer"))])


def test_agent_runs_two_separate_tool_rounds_before_final_answer(monkeypatch, task):
    t.task_message(task, "在工作区写入说明，再读回核对；这些常规操作可以直接进行")
    reviewer = Reviewer("allow", "用户授权在自己的工作区写入并读回")
    model = MultiRoundModel()
    monkeypatch.setattr(runtime, "ChatOpenAI", lambda **kwargs: reviewer if kwargs["max_tokens"] == 1000 else model)
    run(task)
    result = read_record(task)
    assert result["status"] == "completed", result
    assert t.safe_path(f"workspaces/{task}/{model.filename}").read_text() == model.written
    assert len(model.histories) == 3
    # 第一轮未执行工具，第二轮只有写入结果，第三轮才同时看到写入和读回；证明循环观察后继续决策。
    assert [[message.tool_call_id for message in history if isinstance(message, ToolMessage)]
            for history in model.histories] == [[], ["loop-write"], ["loop-write", "loop-read"]]
    events = result["events"]
    assert [(event["callId"], event["name"], event["status"]) for event in events] == [
        ("loop-write", "workspace_patch", "completed"), ("loop-read", "workspace_read", "completed")]
    assert all(entry["status"] == "executed" for entry in result["approvals"])
    assert reviewer.calls == 2
    assert result["summary"] == "已写入并读回：" + model.written
    assert next(message for message in result["messages"] if message["id"] == "loop-final-answer")["content"] == result["summary"]


def test_completed_agent_rebuilds_for_readonly_discussion_without_review_or_tools(monkeypatch, task):
    t.task_message(task, "在工作区写入说明，再读回核对；这些常规操作可以直接进行")
    first_reviewer = Reviewer("allow", "用户授权自己的工作区操作")
    first_model = MultiRoundModel()
    monkeypatch.setattr(runtime, "ChatOpenAI", lambda **kwargs: first_reviewer if kwargs["max_tokens"] == 1000 else first_model)
    run(task)
    completed = read_record(task)
    assert completed["status"] == "completed", completed
    old_approvals = [entry["id"] for entry in completed["approvals"]]
    old_events = [event["id"] for event in completed["events"]]

    # 已完成对话收到普通追问后沿用同一个 Task/thread_id。换一个新模型对象，确保历史来自存档，而非旧模型的内存。
    p.set_mode(task, "readonly")
    question = "只解释一下上一轮两个工具各做了什么，不用继续操作"
    t.task_message(task, question)
    followup = next(message for message in read_record(task)["messages"] if message.get("content") == question)
    discussion = DiscussionModel(expected_question=question)
    unused_reviewer = Reviewer("ask")
    monkeypatch.setattr(runtime, "ChatOpenAI", lambda **kwargs: unused_reviewer if kwargs["max_tokens"] == 1000 else discussion)
    run(task)
    result = read_record(task)
    assert result["status"] == "completed", result
    assert len(discussion.histories) == 1 and unused_reviewer.calls == 0
    history = discussion.histories[0]
    assert [message.tool_call_id for message in history if isinstance(message, ToolMessage)] == ["loop-write", "loop-read"]
    assert sum(isinstance(message, HumanMessage) and message.id == followup["id"] for message in history) == 1
    assert any(isinstance(message, AIMessage) and message.id == "loop-final-answer" for message in history)
    assert [entry["id"] for entry in result["approvals"]] == old_approvals
    assert [event["id"] for event in result["events"]] == old_events
    assert next(message for message in result["messages"] if message["id"] == followup["id"])["delivered"]
    assert result["summary"] == discussion.reply
    assert next(message for message in result["messages"] if message["id"] == "discussion-answer")["content"] == discussion.reply


def test_all_denied_tools_keep_tool_results_out_of_assistant_text(monkeypatch, task):
    t.task_message(task, "这次只讨论，不要写文件")
    reviewer = Reviewer("deny", "用户只要求讨论，不允许写文件")
    model = install_model(monkeypatch, [call()], reviewer)
    run(task)
    result = read_record(task)
    assert result["status"] == "completed", result
    assert reviewer.calls == 1 and not t.safe_path(f"workspaces/{task}/note.txt").exists()
    assert len(result["events"]) == 1 and result["events"][0]["status"] == "blocked"
    # 官方 HITL 可以直接产生拒绝 ToolMessage，跳过真实 handler；它是工具证据，不能被 after_model 当助手正文保存。
    denied = next(message for message in model.histories[-1] if isinstance(message, ToolMessage))
    assert denied.tool_call_id == "call-write" and denied.status == "error"
    assistants = [message for message in result["messages"] if message["role"] == "assistant"]
    assert {message["content"] for message in assistants} == {"准备操作", "已按审批结果处理"}
    assert all(message["content"] != denied.content for message in assistants)
    assert result["summary"] == "已按审批结果处理"
