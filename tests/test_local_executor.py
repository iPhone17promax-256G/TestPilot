import copy
import pytest
from pydantic import ValidationError
from testpilot.config import settings
from testpilot.models import ModelProfile, model_metadata, request_body
from testpilot.executors import merge, register, registry, profile, claim, commit, valid_relative, bundle
from testpilot.storage import Record, session, uid, read_record
from testpilot.workspace import user_scope, project_scope
from testpilot.job_queue import enqueue


def test_reasoning_only_confirmed_metadata():
    data=dict(id="demo:free",pricing={"prompt":"0","completion":"0"},architecture={"input_modalities":["text","image"]},supported_parameters=["tools","reasoning"])
    assert model_metadata(data)["reasoningOptions"]==[]
    data["reasoning"]={"supported_efforts":["high","low","none"],"mandatory":True}
    assert model_metadata(data)["reasoningOptions"]==["low","high"]
    data["reasoning"]={"supported_efforts":None}
    assert "max" in model_metadata(data)["reasoningOptions"]
    data["pricing"]["prompt"]="0.000001"
    assert model_metadata(data)["free"] is False


def test_free_request_has_no_paid_fallback():
    p=dict(provider="openrouter",extraBody={},freeOnly=True,reasoningOptions=["low"])
    body=request_body(p,"low")
    assert body["reasoning"]=={"effort":"low"}
    assert body["provider"]["max_price"]=={"prompt":0,"completion":0}
    with pytest.raises(ValueError):request_body(p,"high")


def test_platform_model_schema_rejects_keys_and_headers():
    with pytest.raises(ValidationError):ModelProfile(name="demo",model="demo",apiKey="secret")
    with pytest.raises(ValueError):ModelProfile(name="demo",model="demo",extraBody={"headers":{"Authorization":"secret"}}).public()
    with pytest.raises(ValueError):ModelProfile(name="demo",model="demo",baseUrl="https://elsewhere.invalid/v1").public()


def test_local_key_is_bound_to_endpoint(monkeypatch,tmp_path):
    from testpilot.local_vault import LocalVault
    monkeypatch.setattr(settings,"client_root",tmp_path)
    vault=LocalVault("USER-A");vault.keyring=None
    vault.put("MODEL-A","secret-demo-key","https://original.invalid/v1")
    assert vault.for_profile(dict(id="MODEL-A",baseUrl="https://original.invalid/v1"))=="secret-demo-key"
    with pytest.raises(ValueError):vault.for_profile(dict(id="MODEL-A",baseUrl="https://other.invalid/v1"))
    other=LocalVault("USER-B");other.keyring=None
    assert other.get("MODEL-A")==""
    assert vault.path.stat().st_mode&0o777==0o600
    vault.delete("MODEL-A")
    assert vault.get("MODEL-A")==""
    with pytest.raises(ValueError):vault.for_profile(dict(id="MODEL-A",baseUrl="https://original.invalid/v1"))


def test_device_session_and_local_action_are_account_scoped():
    from testpilot.storage import list_records
    owner=uid("USER");other=uid("USER")
    with user_scope(owner),session() as db:
        identities=[]
        for kind,field in [("mobile_session","accountId"),("network_session","accountId"),("local_action","ownerId")]:
            identity=uid("PRIVATE");identities.append((identity,kind))
            db.add(Record(id=identity,kind=kind,payload=dict(id=identity,**{field:owner})))
    with user_scope(other):
        for identity,kind in identities:
            with pytest.raises(PermissionError):read_record(identity,kind)
            assert identity not in {r["id"] for r in list_records(kind)}


def test_merge_keeps_new_user_message_and_approval():
    base=dict(messages=[dict(id="m1",role="user",content="old",delivered=False)],approvals=[dict(id="a1",status="pending")])
    changed=copy.deepcopy(base);changed["messages"][0]["delivered"]=True;changed["messages"].append(dict(id="m3",role="assistant",content="response"))
    current=copy.deepcopy(base);current["messages"].append(dict(id="m2",role="user",content="new",delivered=False));current["approvals"][0]["status"]="approved"
    merged=merge(base,changed,current)
    assert [m["id"] for m in merged["messages"]]==["m1","m2","m3"]
    assert merged["messages"][0]["delivered"] is True
    assert merged["approvals"][0]["status"]=="approved"


def test_account_cannot_claim_or_read_other_account():
    owner=uid("USER");other=uid("USER");identity=uid("TASK");model=uid("MODEL")
    with user_scope(owner),session() as db:
        db.add(Record(id=identity,kind="task",status="queued",payload=dict(id=identity,status="queued",ownerId=owner,executorId="",messages=[])))
        enqueue(db,db.get(Record,identity),"agent")
        db.add(Record(id=model,kind="model_profile",payload=dict(id=model,ownerId=owner)))
    with user_scope(other):
        executor=register("","test",{})
        assert claim(executor["id"],"agent") is None
        with pytest.raises(PermissionError):read_record(identity,"task")
        with pytest.raises(ValueError):profile(model)
    with user_scope(owner):
        executor=register("","test",{})
        assert all(e["ownerId"]==owner for e in registry())
        job=claim(executor["id"],"agent")
        assert job["recordId"]==identity
        with project_scope(job["projectId"]):
            snapshot=bundle(job["claim"])
            assert all(not j["claim"] or j["record_id"]==identity for j in snapshot["jobs"])
            row=read_record(identity)
            changed=copy.deepcopy(row);changed["ownerId"]=other
            with pytest.raises(PermissionError):commit(job["claim"],[dict(id=identity,kind="task",base=row,payload=changed)])


@pytest.mark.parametrize("path",["../secrets","/etc/passwd","runs/a/../../x","runs\\a\\x","runs//a/x"])
def test_file_paths_are_relative(path):
    with pytest.raises(ValueError):valid_relative(path)


def test_platform_cannot_run_agent(monkeypatch):
    monkeypatch.setattr(settings,"role","platform")
    from testpilot.agent.runtime import execute_task
    with pytest.raises(PermissionError):execute_task("TASK-NOT-RUN")
    from testpilot.worker import main
    with pytest.raises(RuntimeError):main("agent")


def test_same_concurrent_change_is_not_a_conflict():
    assert merge(False,True,True) is True


def test_readonly_environment_does_not_request_permission():
    from testpilot.agent.permissions import READ_TOOLS
    assert "environment_status" in READ_TOOLS


def test_switching_models_keeps_message_order(monkeypatch):
    from testpilot import models
    from testpilot.testing import take_user_messages
    owner=uid("USER");identity=uid("TASK")
    messages=[dict(id=f"m{i}",role="user",content=str(i),modelProfileId=model,reasoningEffort="",delivered=False) for i,model in enumerate(["A","B","A"])]
    with user_scope(owner),session() as db:
        db.add(Record(id=identity,kind="task",payload=dict(id=identity,ownerId=owner,messages=messages)))
    with user_scope(owner):
        for model,expected in [("A","m0"),("B","m1"),("A","m2")]:
            monkeypatch.setattr(models,"active_selection",(model,""))
            assert [m["id"] for m in take_user_messages(identity)]==[expected]


def test_appium_receives_only_apk_cache(monkeypatch,tmp_path):
    import hashlib
    from testpilot import mobile
    source=tmp_path/"original.apk";source.write_bytes(b"apk-demo")
    monkeypatch.setattr(settings,"role","executor")
    monkeypatch.setattr(settings,"client_root",tmp_path/"local")
    monkeypatch.setattr(mobile,"safe_path",lambda _:source)
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    caps=mobile.capabilities(dict(path="apps/demo.apk",hash=digest),dict(udid="demo",name="demo"))
    target=settings.client_root/"apks"/(digest+".apk")
    assert caps["appium:app"]==str(target)
    assert target.read_bytes()==b"apk-demo"


def test_replica_keeps_job_facts_only_in_authorized_snapshot():
    from testpilot.replica import apply_snapshot
    from testpilot.storage import Base
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    engine=create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db,db.begin():
        apply_snapshot(db,dict(records=[],jobs=[dict(id="PLATFORM",kind="test",record_id="RUN-TEST",status="queued",claim="",lease=None)]))
        assert db.info["jobs"][0]["id"]=="PLATFORM"
        from sqlalchemy import inspect
        assert "jobs" not in inspect(engine).get_table_names()
    engine.dispose()
