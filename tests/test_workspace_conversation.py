import json
from datetime import timedelta
from pathlib import Path
import pytest
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from testpilot.api import app
from testpilot.config import settings
from testpilot.storage import Record, User, LoginSession, ApiToken, session, uid, now, read_record
from testpilot.authentication import password_hash
from testpilot.workspace import project_scope
from testpilot import testing as t
from testpilot.job_queue import enqueue
from queue_helpers import mark_running
from testpilot.steps import compile_steps, validate_steps

@pytest.fixture(scope="module")
def client():
    yield TestClient(app, base_url="https://testserver",headers={"Authorization":"Bearer "+settings.access_token})

@pytest.fixture
def projects(client):
    identities=[]
    for name in ["测试隔离 A","测试隔离 B"]:
        response=client.post("/api/projects",json={"name":name})
        assert response.status_code==200
        identities.append(response.json()["id"])
    yield identities
    with session() as db:
        for row in db.scalars(select(Record).where((Record.project_id.in_(identities)) | (Record.id.in_(identities)))):
            db.delete(row)

def test_project_boundaries_and_idempotency(client,projects):
    a,b=projects
    body={"arguments":{"name":"隔离用例","kind":"api","steps":[{"action":"request","path":"/"}]},"key":"same-key"}
    ra=client.post("/api/tools/cases_save_revision",headers={"X-TestPilot-Project":a},json=body)
    rb=client.post("/api/tools/cases_save_revision",headers={"X-TestPilot-Project":b},json=body)
    assert ra.status_code==rb.status_code==200
    assert ra.json()["id"]!=rb.json()["id"]
    state=client.get("/api/state",headers={"X-TestPilot-Project":b}).json()
    assert [c["id"] for c in state["cases"]]==[rb.json()["id"]]
    denied=client.post("/api/tools/cases_read",headers={"X-TestPilot-Project":b},json={"arguments":{"id":ra.json()["id"]}})
    assert denied.status_code==404
    body["arguments"]["case_id"]=ra.json()["id"]
    assert client.post("/api/tools/cases_save_revision",headers={"X-TestPilot-Project":b},json=body).status_code==404

def test_environment_cannot_cross_projects(client,projects):
    a,b=projects
    environment=client.post("/api/environments",headers={"X-TestPilot-Project":a},json={"name":"A","url":"http://target.test"}).json()
    case=client.post("/api/tools/cases_save_revision",headers={"X-TestPilot-Project":b},json={"arguments":{"name":"B","kind":"api","steps":[{"action":"request","path":"/"}]}}).json()
    response=client.post("/api/runs",headers={"X-TestPilot-Project":b},json={"case_id":case["id"],"revision_number":1,"environment_id":environment["id"]})
    assert response.status_code==404

def test_running_conversation_accepts_idempotent_followup(client,projects):
    project=projects[0];identity=uid("TASK")
    with project_scope(project),session() as db:
        db.add(Record(id=identity,kind="task",status="running",payload=dict(id=identity,status="running",messages=[],events=[],runIds=[],caseIds=[],goal="原任务",createdAt=t.stamp())))
        enqueue(db,db.get(Record,identity),"agent")
    mark_running(identity)
    headers={"X-TestPilot-Project":project}
    for _ in range(2):
        response=client.post(f"/api/tasks/{identity}/messages",headers=headers,json={"content":"先别执行，解释一下","key":"follow-up"})
        assert response.status_code==202
    with project_scope(project):
        messages=read_record(identity)["messages"]
        assert len(messages)==1 and not messages[0]["delivered"]
        assert t.take_user_messages(identity)[0]["content"]=="先别执行，解释一下"
        assert t.take_user_messages(identity)==[]

def test_tool_event_updates_instead_of_duplicating(projects):
    identity=uid("TASK")
    with project_scope(projects[0]),session() as db:
        db.add(Record(id=identity,kind="task",payload=dict(id=identity,status="running",messages=[],events=[])))
    with project_scope(projects[0]):
        t.task_event(identity,"cases_list","{}","执行中","call1","running")
        t.task_event(identity,"cases_list","{}","[]","call1","completed")
        task=read_record(identity)
        assert len(task["events"])==len(task["messages"])==1
        assert task["events"][0]["status"]=="completed"

def test_real_account_session_and_logout(client):
    user_id=uid("USER");username="test_"+user_id[-8:];password="AcceptanceOnly2026!"
    with session() as db:db.add(User(id=user_id,username=username,password_hash=password_hash(password)))
    try:
        anonymous=TestClient(app, base_url="https://testserver")
        assert anonymous.get("/api/state").status_code==401
        assert anonymous.post("/api/auth/login",json={"username":username,"password":"WrongPassword"}).status_code==401
        response=anonymous.post("/api/auth/login",json={"username":username,"password":password})
        assert response.status_code==200 and "HttpOnly" in response.headers["set-cookie"]
        assert anonymous.get("/api/state").status_code==200
        token=anonymous.post("/api/auth/tokens",json={"name":"pytest"}).json()
        assert client.get("/api/tools",headers={"Authorization":"Bearer "+token["token"]}).status_code==200
        assert anonymous.post("/api/auth/tokens/"+token["id"]+"/revoke",json={}).status_code==200
        assert client.get("/api/tools",headers={"Authorization":"Bearer "+token["token"]}).status_code==401
        assert anonymous.post("/api/auth/logout",json={}).status_code==200
        assert anonymous.get("/api/state").status_code==401
    finally:
        with session() as db:
            db.execute(delete(LoginSession).where(LoginSession.user_id==user_id))
            db.execute(delete(ApiToken).where(ApiToken.user_id==user_id))
            db.execute(delete(User).where(User.id==user_id))

def test_visual_api_extracts_variable_into_headers(tmp_path,monkeypatch):
    from testpilot.runners import steps as runtime
    from testpilot.steps import validate_steps
    seen=[]
    def target(request):
        seen.append(request)
        if request.url.path=="/login":return httpx.Response(200,json={"data":{"token":"secret-token"}},headers={"Token":"secret-token"})
        assert request.headers["Authorization"]=="Bearer secret-token"
        return httpx.Response(200,json={"name":"tester"})
    original=httpx.Client
    monkeypatch.setattr(runtime.httpx,"Client",lambda **kwargs:original(transport=httpx.MockTransport(target),**kwargs))
    monkeypatch.setattr(runtime,"Path",lambda value:tmp_path/"steps.json")
    steps=validate_steps("api",[
        {"action":"request","path":"/login"},
        {"action":"extract","source":"json","expression":"data.token","variable":"token"},
        {"action":"request","path":"/me","headers":{"Authorization":"Bearer {{token}}"}},
        {"action":"assert","source":"json","expression":"name","expected":"tester"}])
    runtime.execute_steps("api",steps,"http://target.test")
    report=json.loads((tmp_path/"steps.json").read_text())
    assert all(r["status"]=="passed" for r in report)
    assert "secret-token" not in (tmp_path/"steps.json").read_text()
    assert len(seen)==2

def test_visual_failure_has_step_and_does_not_continue(tmp_path,monkeypatch):
    from testpilot.runners import steps as runtime
    original=httpx.Client;seen=[]
    def target(request):
        seen.append(request);return httpx.Response(200,json={"status":"OK"})
    monkeypatch.setattr(runtime.httpx,"Client",lambda **kwargs:original(transport=httpx.MockTransport(target),**kwargs))
    monkeypatch.setattr(runtime,"Path",lambda value:tmp_path/"steps.json")
    steps=validate_steps("api",[{"action":"request","path":"/"},{"action":"assert","name":"检查业务状态","source":"json","expression":"status","expected":"wrong"},{"action":"request","path":"/after"}])
    with pytest.raises(AssertionError,match="第 2 步"):runtime.execute_steps("api",steps,"http://target.test")
    assert len(seen)==1
    assert json.loads((tmp_path/"steps.json").read_text())[-1]["status"]=="failed"

@pytest.mark.parametrize("kind,steps",[
 ("web",[{"action":"fill","by":"label","selector":"账号","value":"{{env.TEST_USERNAME}}"}]),
 ("app",[{"action":"click","by":"accessibility id","selector":"Views"}]),
 ("api",[{"action":"request","path":"/ping"},{"action":"assert","source":"status","expected":200}]),
 ("performance",[{"action":"request","path":"/ping","expected_status":200}]),
])
def test_visual_compiles_to_native_assets(kind,steps):
    normalized,code=compile_steps(kind,steps)
    assert normalized[0]["id"]
    if kind=="performance":assert "<jmeterTestPlan" in code
    else:compile(code,"test_case.py","exec")

def test_bad_step_and_missing_variable_are_rejected():
    from testpilot.runners.steps import substitute,json_value
    with pytest.raises(ValueError):validate_steps("api",[{"action":"click","selector":"foo"}])
    with pytest.raises(ValueError):substitute("Bearer {{missing}}",{})
    assert json_value({"data":{"users":[{"id":1}]}},"$.data.users[0].id")==1


def test_protocol_expected_values_are_numeric():
    assert validate_steps("api",[{"action":"assert","source":"status","expected":"200"}])[0]["expected"]==200


def test_request_validation_does_not_echo_password(client):
    response=client.post("/api/auth/login",json={"username":"bad","password":"p!x"})
    assert response.status_code==422
    assert "p!x" not in response.text


def test_state_uses_conditional_requests(client):
    response=client.get("/api/state")
    etag=response.headers["etag"]
    cached=client.get("/api/state",headers={"If-None-Match":etag})
    assert cached.status_code==304 and not cached.content


def test_stop_suppresses_old_pending_messages_but_allows_new_followup(client,projects):
    identity=uid("TASK")
    with project_scope(projects[0]),session() as db:
        db.add(Record(id=identity,kind="task",status="running",payload=dict(id=identity,status="running",messages=[dict(id="old",role="user",content="执行测试",delivered=False)],events=[],runIds=[],caseIds=[],goal="原任务",createdAt=t.stamp())))
        enqueue(db,db.get(Record,identity),"agent")
    mark_running(identity)
    with project_scope(projects[0]):
        t.task_cancel(identity)
        t.task_message(identity,"解释一下，别再执行")
        messages=read_record(identity)["messages"]
        assert messages[0]["suppressed"] and messages[0]["delivered"]
        assert not messages[1]["delivered"]
        assert t.take_user_messages(identity)[0]["content"]=="解释一下，别再执行"


def test_optional_http_status_on_request_is_actually_asserted(tmp_path,monkeypatch):
    from testpilot.runners import steps as runtime
    original=httpx.Client
    monkeypatch.setattr(runtime.httpx,"Client",lambda **kwargs:original(transport=httpx.MockTransport(lambda req:httpx.Response(401,json={})),**kwargs))
    monkeypatch.setattr(runtime,"Path",lambda value:tmp_path/"steps.json")
    steps=validate_steps("api",[{"action":"request","path":"/","expected_status":200}])
    with pytest.raises(AssertionError,match="401"):runtime.execute_steps("api",steps,"http://target.test")
    assert validate_steps("api",[{"action":"request","path":"/"}])[0]["expected_status"] is None
