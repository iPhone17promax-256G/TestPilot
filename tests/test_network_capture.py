"""Real traffic and session isolation checks for browser network evidence."""
import asyncio
import ipaddress
import json
import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.sync_api import sync_playwright
from sqlalchemy import select
from aiqa.network_capture import NetworkRecorder, atomic_json
from aiqa.network import create_session, network_list, network_read, capture_set, task_policy, resolve_level, session_summary
from aiqa.storage import Record, session, uid, update_record
from aiqa.config import settings
from aiqa.workspace import project_scope
from aiqa.tools import dispatch


class TargetHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/api/drop":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if self.path == "/":
            payload, mime, status = b"<html><title>Network test</title><body>Target</body></html>", "text/html", 200
        elif self.path == "/asset":
            payload, mime, status = b"binary-data", "application/octet-stream", 200
        elif self.path == "/api/big":
            payload, mime, status = b"x" * 70000, "text/plain", 200
        elif self.path == "/api/error":
            payload, mime, status = b'{"message":"invalid account"}', "application/json", 401
        else:
            payload, mime, status = b'{"status":"OK"}', "application/json", 200
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        content = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


@pytest.fixture
def target(tmp_path):
    servers = []
    def start(https=False):
        server = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        if https:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            now = datetime.now(timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                    .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                    .not_valid_after(now + timedelta(days=1))
                    .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                    .sign(key, hashes.SHA256()))
            cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
            cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_file, key_file)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return ("https" if https else "http") + "://127.0.0.1:" + str(server.server_port)
    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("https", [False, True])
def test_http_https_capture_bodies_failures_and_context_isolation(target, tmp_path, https):
    url = target(https)
    with sync_playwright() as engine:
        browser = engine.chromium.launch(executable_path="/usr/lib/chromium/chromium", args=["--no-sandbox"])
        own = browser.new_context(ignore_https_errors=https)
        unrelated = browser.new_context(ignore_https_errors=https)
        recorder = NetworkRecorder(tmp_path / "network", "NET-TEST", url)
        recorder.bind_sync(own)
        page = own.new_page()
        page.goto(url)
        other = unrelated.new_page()
        other.goto(url)
        other.evaluate("fetch('/api/unrelated')")
        page.evaluate("async () => {await fetch('/api/status');await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'demo'})});await fetch('/api/error');await fetch('/api/big');await fetch('/api/drop').catch(()=>{});}")
        page.wait_for_timeout(150)
        browser.close()
        # Match pytest's teardown order: browser fixtures end before the
        # reporting hook. A closed context must not break test reporting.
        recorder.close()
    index = json.loads((tmp_path / "network/index.json").read_text())
    assert index["status"] == "completed"
    assert not any("/api/unrelated" in r["url"] for r in index["requests"])
    assert all(r["resourceType"] == "fetch" for r in index["requests"])
    records = [json.loads((tmp_path / "network" / (r["id"] + ".json")).read_text()) for r in index["requests"]]
    login = next(r for r in records if r["url"].endswith("/api/login"))
    assert json.loads(login["requestBody"]) == json.loads(login["responseBody"]) == {"username": "demo"}
    assert login["method"] == "POST" and login["status"] == 200 and login["durationMs"] >= 0
    assert next(r for r in records if r["url"].endswith("/api/error"))["status"] == 401
    assert next(r for r in records if r["url"].endswith("/api/drop"))["state"] == "failed"
    big = next(r for r in records if r["url"].endswith("/api/big"))
    assert not big["responseBody"] and "超过" in big["responseBodyNote"]
    assert not any("responseBody" in r for r in index["requests"])


@pytest.mark.parametrize("level", ["off", "detailed"])
def test_off_has_no_listener_and_detailed_keeps_resource_summaries(target, tmp_path, level):
    url = target()
    with sync_playwright() as engine:
        browser = engine.chromium.launch(executable_path="/usr/lib/chromium/chromium", args=["--no-sandbox"])
        context = browser.new_context()
        recorder = NetworkRecorder(tmp_path / "network", "NET-TEST", url, level)
        recorder.bind_sync(context)
        page = context.new_page()
        page.goto(url)
        page.evaluate("async()=>{await fetch('/asset');await fetch('/api/big')}")
        page.wait_for_timeout(100)
        recorder.close()
        browser.close()
    index = json.loads((tmp_path / "network/index.json").read_text())
    if level == "off":
        assert not index["requests"] and not recorder.contexts
    else:
        assert any(r["resourceType"] == "document" for r in index["requests"])
        records = [json.loads((tmp_path / "network" / (r["id"] + ".json")).read_text()) for r in index["requests"]]
        assert len(next(r for r in records if r["url"].endswith("/api/big"))["responseBody"]) == 70000
        assert not next(r for r in records if r["url"].endswith("/asset"))["responseBody"]


def test_capture_origin_and_request_count_limits(monkeypatch, tmp_path):
    import aiqa.network_capture as capture
    monkeypatch.setattr(capture, "MAX_REQUESTS", 2)
    recorder = NetworkRecorder(tmp_path, "NET-TEST", "https://target.test")
    class Request:
        url = "https://target.test/api/status"
        resource_type = "fetch"
        method = "GET"
        headers = {}
        post_data = None
        failure = "failed"
    recorder._request(Request())
    recorder._request(Request())
    recorder._request(Request())
    other = Request()
    other.url = "https://other.test/api/status"
    recorder._request(other)
    recorder.close()
    index = json.loads((tmp_path / "index.json").read_text())
    assert len(index["requests"]) == 2 and index["dropped"] == index["filtered"] == 1
    assert all(r["state"] == "incomplete" for r in index["requests"])


def test_storage_budget_includes_index_and_omission_is_explicit(monkeypatch, tmp_path):
    import aiqa.network_capture as capture
    monkeypatch.setattr(capture, "MAX_DETAIL_BYTES", 1200)
    recorder = NetworkRecorder(tmp_path, "NET-TEST", "http://target.test")
    class Request:
        url = "http://target.test/api/status"
        resource_type = "fetch"
        method = "POST"
        headers = {}
        post_data = "x" * 2000
        failure = None
    request = Request()
    recorder._request(request)
    recorder._finished(request, b"response")
    recorder.close()
    index = json.loads((tmp_path / "index.json").read_text())
    assert index["bytes"] == sum(p.stat().st_size for p in tmp_path.glob("*.json"))
    record = json.loads((tmp_path / "REQ-0001.json").read_text())
    assert not record["requestBody"] and "上限" in record["requestBodyNote"]


def test_browser_use_owned_cdp_session_can_be_observed_without_model(target, tmp_path):
    url = target()
    async def exercise():
        from browser_use import Browser
        from playwright.async_api import async_playwright
        browser = Browser(headless=True, executable_path="/usr/lib/chromium/chromium",
                          args=["--no-sandbox", "--disable-dev-shm-usage"], enable_default_extensions=False, user_data_dir=None)
        observer = None
        recorder = NetworkRecorder(tmp_path, "NET-TEST", url)
        try:
            await browser.start()
            observer = await async_playwright().start()
            attached = await observer.chromium.connect_over_cdp(browser.cdp_url)
            for context in attached.contexts:
                recorder.bind_async(context)
            native = await browser.get_or_create_cdp_session()
            await native.cdp_client.send.Page.navigate(params={"url": url}, session_id=native.session_id)
            await asyncio.sleep(.3)
            await native.cdp_client.send.Runtime.evaluate(params={"expression": "fetch('/api/status').then(r=>r.text())", "awaitPromise": True}, session_id=native.session_id)
            await asyncio.sleep(.1)
            await recorder.aclose()
        finally:
            if observer:
                await observer.stop()
            await browser.kill()
    asyncio.run(exercise())
    index = json.loads((tmp_path / "index.json").read_text())
    assert any(r["url"].endswith("/api/status") for r in index["requests"])
    record = json.loads((tmp_path / (index["requests"][0]["id"] + ".json")).read_text())
    assert json.loads(record["responseBody"]) == {"status": "OK"}


@pytest.fixture
def captures(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data", tmp_path)
    a, b, own, other = uid("PROJECT"), uid("PROJECT"), uid("TASK"), uid("TASK")
    with session() as db:
        for identity in [a, b]:
            db.add(Record(id=identity, kind="project", payload={"id": identity, "name": identity}))
    results = []
    for project, task in [(a, own), (a, other), (b, uid("TASK"))]:
        with project_scope(project), session() as db:
            db.add(Record(id=task, kind="task", payload=dict(id=task, status="running", messages=[dict(id="turn1", role="user", content="测试", captureLevel=None)])))
        with project_scope(project):
            identity = uid("INS")
            result = create_session(identity, "inspection", "inspections/" + identity, "http://target.test", "api", task)
            folder = tmp_path / result["path"]
            folder.mkdir(parents=True)
            atomic_json(folder / "index.json", dict(status="completed", requests=[dict(id="REQ-0001", method="GET", url="http://target.test/api/status", state="finished", status=200)], filtered=0, dropped=0, bytes=200, limits={}))
            atomic_json(folder / "REQ-0001.json", dict(id="REQ-0001", requestBody="", responseBody="abcdefghij", requestBodyNote="", responseBodyNote="", requestHeaders={"x-test":"yes"}))
            results.append(result)
    yield tmp_path, a, b, own, other, results
    with session() as db:
        for row in db.scalars(select(Record).where(Record.project_id.in_([a, b]) | Record.id.in_([a, b]))):
            db.delete(row)


def test_agent_network_tools_are_task_scoped_and_bodies_are_paged(captures):
    _, a, b, own, other, records = captures
    with project_scope(a):
        sessions = dispatch("network_list", {}, task_id=own)
        assert [r["id"] for r in sessions["sessions"]] == [records[0]["id"]]
        rows = dispatch("network_list", {"session_id": records[0]["id"]}, task_id=own)
        assert rows["total"] == 1 and "responseBody" not in rows["requests"][0]
        page = dispatch("network_read", dict(session_id=records[0]["id"], request_id="REQ-0001", section="response_body", offset=2, limit=3), task_id=own)
        assert page["content"] == "cde" and page["nextOffset"] == 5
        with pytest.raises(ValueError):
            dispatch("network_read", dict(session_id=records[1]["id"], request_id="REQ-0001"), task_id=own)
        with pytest.raises(ValueError):
            dispatch("network_list", {"session_id": records[2]["id"]}, task_id=own)
        with pytest.raises(ValueError):
            network_read(records[0]["id"], "../REQ-0001")
    with project_scope(b), pytest.raises(ValueError):
        network_list(records[0]["id"])


def test_user_off_cannot_be_overridden_and_next_turn_resets(captures):
    _, a, _, own, other, records = captures
    with project_scope(a):
        assert task_policy(own)["level"] == "api"
        assert capture_set(own, "detailed", "排查响应正文")["level"] == "detailed"
        assert resolve_level(other) == "api"
        update_record(own, lambda p: p.update(messages=[*p["messages"], dict(id="turn2", role="user", content="关闭", captureLevel="off")]))
        assert resolve_level(own, "detailed") == "off"
        with pytest.raises(ValueError, match="不能重新开启"):
            capture_set(own, "api", "重新开启")
        update_record(own, lambda p: p.update(messages=[*p["messages"], dict(id="turn3", role="user", content="继续", captureLevel=None)]))
        assert task_policy(own)["level"] == "api" and not task_policy(own)["locked"]


def test_network_folder_symlink_cannot_cross_session(captures):
    root, a, _, own, _, records = captures
    path = root / records[0]["path"]
    assert path.resolve().is_relative_to(root.resolve())
    for name in ["index.json", "REQ-0001.json"]:
        (path / name).unlink()
    path.rmdir()
    path.symlink_to(root / records[1]["path"], target_is_directory=True)
    with project_scope(a), pytest.raises(ValueError, match="超出"):
        network_read(records[0]["id"], "REQ-0001", task_id=own)


def test_unfinished_capture_preserves_evidence_and_is_not_reported_running(captures):
    root, a, _, own, _, records = captures
    capture = records[0]
    path = root / capture["path"] / "index.json"
    index = json.loads(path.read_text())
    index["status"] = "running"
    atomic_json(path, index)
    with project_scope(a):
        summary = session_summary(capture["id"], own, finish=True)
        assert summary["status"] == "interrupted" and summary["count"] == 1
        assert session_summary(capture["id"], own)["status"] == "interrupted"
        assert network_read(capture["id"], "REQ-0001", "response_body", task_id=own)["content"] == "abcdefghij"


def test_recorder_failure_does_not_replace_the_pytest_verdict(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from aiqa.runners import pytest_plugin
    class BrokenRecorder:
        def close(self):
            raise RuntimeError("evidence unavailable")
    monkeypatch.setattr(pytest_plugin, "network_recorder", BrokenRecorder())
    monkeypatch.setattr(pytest_plugin, "Path", lambda _: tmp_path / "results.json")
    monkeypatch.setattr(pytest_plugin, "items", [{"phase": "call", "outcome": "failed"}])
    pytest_plugin.pytest_sessionfinish(SimpleNamespace(testscollected=1), 1)
    result = json.loads((tmp_path / "results.json").read_text())
    assert result["exitCode"] == 1 and result["collected"] == 1
    assert result["items"][0]["outcome"] == "failed"
