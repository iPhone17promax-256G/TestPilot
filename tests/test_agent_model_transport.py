"""Exercise real ChatOpenAI HTTP/SSE, tool calls and approval restoration locally."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from testpilot import testing as testing
from testpilot.agent import permissions, runtime
from testpilot.config import settings
from testpilot.storage import Record, read_record, session, update_record
from testpilot.workspace import project_scope


@pytest.fixture
def conversation():
    # 业务数据只进入验收脚本创建的临时数据库；项目与任务在结束后清理。
    project = testing.project_save("Agent HTTP 流式验收")
    with project_scope(project["id"]):
        environment = testing.environment_save("临时目标", "http://target.test")
        task = testing.task_start("先读取已有用例，再保存说明；写入前让我确认", environment["id"])
        update_record(task["id"], lambda payload: payload.update(status="running"))
        yield task["id"]
    with session() as database:
        for row in database.scalars(select(Record).where(
            (Record.project_id == project["id"]) | (Record.id == project["id"])
        )):
            database.delete(row)


@pytest.fixture
def model_endpoint():
    # 模拟供应商只绑定容器内回环地址。产品仍使用真实 ChatOpenAI 客户端，
    # 所有模型请求通过 HTTP/SSE，测试不请求外部供应商，也不记录请求密钥。
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.requests.append(body)
            usage = dict(prompt_tokens=10, completion_tokens=5, total_tokens=15)
            if not body.get("stream"):
                evidence = json.loads(body["messages"][-1]["content"])
                decision = "ask" if evidence["tool"] == "workspace_patch" else "allow"
                message = dict(role="assistant", content=json.dumps(
                    dict(decision=decision, reason="遵循用户对写入前确认的要求"), ensure_ascii=False
                ))
                response = dict(id="chatcmpl-review", object="chat.completion", created=1,
                                model=body["model"], usage=usage,
                                choices=[dict(index=0, message=message, finish_reason="stop")])
                raw = json.dumps(response, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            answered = {message["tool_call_id"] for message in body["messages"]
                        if message["role"] == "tool"}
            call = None
            if "http-read" not in answered:
                call = ("http-read", "cases_list", {})
                content = "先查询已有用例。"
            elif "http-write" not in answered:
                call = ("http-write", "workspace_patch", dict(path="http-note.txt", content="HTTP 流式验证"))
                content = "准备保存说明，等待确认。"
            else:
                write_result = next(message for message in body["messages"]
                                    if message["role"] == "tool" and message["tool_call_id"] == "http-write")
                content = ("按你的拒绝没有保存说明，验证结束。"
                           if json.loads(write_result["content"]).get("notExecuted")
                           else "已经按你的批准保存说明，验证结束。")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            identity = "chatcmpl-" + str(len(self.server.requests))

            def chunk(delta, finish=None, reported_usage=None):
                response = dict(id=identity, object="chat.completion.chunk", created=1,
                                model=body["model"], choices=[] if reported_usage else [
                                    dict(index=0, delta=delta, finish_reason=finish)])
                if reported_usage:
                    response["usage"] = reported_usage
                self.wfile.write(("data: " + json.dumps(response, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()

            # 将正文和 JSON 参数拆成多个 SSE 片段，检查 LangChain 能合并
            # tool_call_chunks，并在暂停/恢复后保留完整参数与原 call ID。
            chunk(dict(role="assistant", content=content[:4]))
            chunk(dict(content=content[4:]))
            if call:
                call_id, name, arguments = call
                encoded = json.dumps(arguments, ensure_ascii=False)
                middle = max(1, len(encoded) // 2)
                chunk(dict(tool_calls=[dict(index=0, id=call_id, type="function",
                                            function=dict(name=name, arguments=encoded[:middle]))]))
                chunk(dict(tool_calls=[dict(index=0, function=dict(arguments=encoded[middle:]))]))
            chunk({}, finish="tool_calls" if call else "stop")
            chunk({}, reported_usage=usage)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_real_http_streaming_tools_pause_resume_and_chat_messages(monkeypatch, conversation, model_endpoint, decision):
    endpoint = f"http://127.0.0.1:{model_endpoint.server_port}/v1"
    monkeypatch.setattr(runtime, "model_settings", lambda: dict(model="local-protocol-test", baseUrl=endpoint, maxSteps=12))
    monkeypatch.setattr(settings, "model_key", "local-test-placeholder")
    monkeypatch.setattr(settings, "model_reasoning_effort", "")
    extensions = SimpleNamespace(scrub=lambda value: value, tools=lambda: [], rows=[], statuses={})
    runtime._execute_task(conversation, extensions, [])

    paused = read_record(conversation)
    assert paused["status"] == "waiting_approval", paused
    assert not testing.safe_path(f"workspaces/{conversation}/http-note.txt").exists()
    approvals = {entry["callId"]: entry for entry in paused["approvals"]}
    assert approvals["http-read"]["status"] == "executed"
    assert approvals["http-write"]["status"] == "pending"
    assert approvals["http-write"]["interruptId"]
    assert any(message.get("content") == "准备保存说明，等待确认。" for message in paused["messages"])
    assert not any('"decision"' in message.get("content", "") for message in paused["messages"] if message["role"] == "assistant")

    # 新 runtime 会重建供应商客户端和会话连接；恢复直接执行已批准的调用，
    # 不重新生成那次写入请求，不再次调用审批模型，再把真实结果交回主模型。
    permissions.decide(conversation, approvals["http-write"]["id"], decision)
    update_record(conversation, lambda payload: payload.update(status="running"))
    runtime._execute_task(conversation, extensions, [])
    finished = read_record(conversation)
    assert finished["status"] == "completed", finished
    path = testing.safe_path(f"workspaces/{conversation}/http-note.txt")
    assert path.exists() == (decision == "approve")
    if decision == "approve":
        assert path.read_text() == "HTTP 流式验证"
    assert len(finished["events"]) == 2
    statuses = {event["callId"]: event["status"] for event in finished["events"]}
    assert statuses == {"http-read": "completed", "http-write": "completed" if decision == "approve" else "blocked"}
    assert finished["summary"] == ("已经按你的批准保存说明，验证结束。" if decision == "approve"
                                   else "按你的拒绝没有保存说明，验证结束。")
    assert finished["approvalUsage"]["requests"] == 2
    assert finished["approvalUsage"]["totalTokens"] == 30
    assert finished["usage"]["totalTokens"] == 45
    assert not any(message.get("partial") for message in finished["messages"] if message["role"] == "assistant")
    main_requests = [body for body in model_endpoint.requests if body.get("stream")]
    assert len(main_requests) == 3
    assert {message["tool_call_id"] for message in main_requests[-1]["messages"] if message["role"] == "tool"} == {"http-read", "http-write"}
    requested = {call["id"] for message in main_requests[-1]["messages"]
                 for call in message.get("tool_calls", [])}
    assert requested == {"http-read", "http-write"}
    assert any(tool["function"]["name"] == "workspace_patch" for tool in main_requests[0]["tools"])
