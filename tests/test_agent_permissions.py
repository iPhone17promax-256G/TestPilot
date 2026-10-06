"""Real checkpoint pause/resume against the disposable MySQL test database."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from sqlalchemy import select

from aiqa import testing as t
from aiqa.agent import permissions as p, runtime
from aiqa.api import app
from aiqa.config import settings
from aiqa.storage import Job, Record, read_record, session, uid, update_record
from aiqa.tools import dispatch
from aiqa.workspace import project_scope


class FakeModel(BaseChatModel):
    calls: list[dict] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "permission-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
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
            for job in db.scalars(select(Job).where(Job.record_id == row.id)):
                db.delete(job)
            db.delete(row)


def call(name="workspace_patch", arguments=None, identity="call-write"):
    return dict(name=name, args=arguments if arguments is not None else dict(path="note.txt", content="AIQA approval test"), id=identity, type="tool_call")


def extension():
    return SimpleNamespace(scrub=lambda value: value, tools=lambda: [], rows=[], statuses={})


def install_model(monkeypatch, calls, reviewer):
    model = FakeModel(calls=calls)
    monkeypatch.setattr(runtime, "ChatOpenAI", lambda **kwargs: reviewer if kwargs["max_tokens"] == 1000 else model)


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
    p.decide(task, entry["id"], "approve")  # double click is idempotent
    # New runtime and saver connection simulate a different Worker process.
    run(task)
    result = read_record(task)
    assert result["status"] == "completed", result
    assert result["approvals"][0]["status"] == "executed"
    assert t.safe_path(f"workspaces/{task}/note.txt").read_text() == "AIQA approval test"
    assert len(result["events"]) == 1
    assert json.loads(result["events"][0]["result"])["_permission"]["approvedBy"] == "user"
    assert reviewer.calls == 1
    assert result["approvalUsage"]["totalTokens"] == 20
    assert not p.check(task, call())[0]  # replay cannot repeat side effects


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
    from aiqa.mcp_client import MCPHub
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
    assert not p.check(task, call(arguments=dict(path="other.txt", content="AIQA approval test")))[0]
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
    client = TestClient(app, headers={"Authorization": "Bearer " + settings.access_token, "X-AIQA-Project": read_record(task)["projectId"]})
    assert client.post(f"/api/tasks/{task}/approvals/{entry['id']}", json={"decision": "reject"}).status_code == 202
    assert client.post(f"/api/tasks/{task}/approvals/{entry['id']}", json={"decision": "approve"}).status_code == 409
    assert client.post(f"/api/tasks/{task}/permissions", headers={"X-AIQA-Project": "PROJECT"}, json={"mode": "bypass"}).status_code == 404


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
    # Mimic Worker claim; the approval decision can arrive before its finally block.
    with session() as db:
        job = db.scalar(select(Job).where(Job.record_id == task))
        job.status = "running"
        job.claim = "test-claim"
    runtime._execute_task(task, extension(), [])
    paused = read_record(task)
    assert paused["status"] == "waiting_approval"
    client = TestClient(app, headers={"Authorization": "Bearer " + settings.access_token, "X-AIQA-Project": paused["projectId"]})
    snapshot = client.get("/api/state").json()
    reloaded = next(v for v in snapshot["tasks"] if v["id"] == task)
    assert reloaded["approvals"][0]["interruptId"] == paused["approvals"][0]["interruptId"]
    p.decide(task, paused["approvals"][0]["id"], "approve")
    with session() as db:
        job = db.scalar(select(Job).where(Job.record_id == task))
        assert job.status == "running" and job.claim == "test-claim"
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
