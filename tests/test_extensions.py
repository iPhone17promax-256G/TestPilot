import io
import json
import zipfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from aiqa.api import app
from aiqa.config import settings
from aiqa.extensions import SkillInput,MCPInput,skill_import,parse_skill,credentials
from aiqa.storage import Record,Job,session,read_record
from aiqa.workspace import project_scope
from aiqa.tools import dispatch

CONTENT="---\nname: acceptance-method\ndescription: 用于扩展能力验收。\n---\n\n先核对需求，再执行测试。\n"

@pytest.fixture
def workspace(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,"data",tmp_path)
    client=TestClient(app,headers={"Authorization":"Bearer "+settings.access_token})
    projects=[client.post("/api/projects",json={"name":"扩展隔离测试 "+x}).json()["id"] for x in ["A","B"]]
    yield client,projects
    with session() as db:
        for row in db.scalars(select(Record).where((Record.project_id.in_(projects)) | (Record.id.in_(projects)))):
            for job in db.scalars(select(Job).where(Job.record_id==row.id)):db.delete(job)
            db.delete(row)

def headers(project):return {"X-AIQA-Project":project}

def test_custom_skill_read_resources_paging_and_toggle(workspace):
    client,(a,b)=workspace
    response=client.post("/api/extensions/skills",headers=headers(a),json={"content":CONTENT,"resources":{"scripts/check.py":"print('optional')"}})
    assert response.status_code==200,response.text
    identity=response.json()["id"]
    with project_scope(a):
        catalog=dispatch("skills_list",{})
        assert next(s for s in catalog if s["name"]=="acceptance-method")["resources"]==["SKILL.md","scripts/check.py"]
        first=dispatch("skills_read",{"name":"acceptance-method","limit":8})
        assert first["content"]==CONTENT[:8] and first["nextOffset"]==8
        assert dispatch("skills_read",{"name":"acceptance-method","resource":"scripts/check.py"})["content"]=="print('optional')"
        with pytest.raises(ValueError):dispatch("skills_read",{"name":"acceptance-method","resource":"../other/SKILL.md"})
    assert client.get("/api/extensions/skills/"+identity,headers=headers(b)).status_code==404
    assert client.post("/api/extensions/skills/"+identity+"/enabled",headers=headers(b),json={"enabled":False}).status_code==404
    assert client.post("/api/extensions/skills/"+identity+"/enabled",headers=headers(a),json={"enabled":False}).status_code==200
    with project_scope(a):
        assert all(s["name"]!="acceptance-method" for s in dispatch("skills_list",{}))
        with pytest.raises(ValueError):dispatch("skills_read",{"name":"acceptance-method"})
    assert client.post("/api/extensions/skills/"+identity+"/delete",headers=headers(a),json={}).status_code==200

@pytest.mark.parametrize("text",[
    "# no metadata",
    "---\nname: [bad\ndescription: description\n---\nbody",
    "---\nname: invalid_Name\ndescription: description\n---\nbody",
    "---\nname: api-testing\ndescription: description\n---\nbody",
    "---\nname: valid-name\ndescription: 123\n---\nbody",
    "---\nname: valid-name\ndescription: description\n---\n",
    "---\nname: &a [*a]\ndescription: description\n---\nbody",
])
def test_skill_rejects_invalid_metadata(text):
    with pytest.raises(ValueError):parse_skill(text)

def zip_bytes(files):
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w") as archive:
        for name,content in files.items():archive.writestr(name,content)
    return output.getvalue()

def test_skill_zip_import_directory_and_reject_traversal(workspace):
    _,(a,b)=workspace
    with project_scope(a):
        result=skill_import("method.zip",zip_bytes({"method/SKILL.md":CONTENT,"method/scripts/helper.py":"print(1)"}))
        assert result["name"]=="acceptance-method"
        assert "scripts/helper.py" in result["files"]
        with pytest.raises(ValueError):skill_import("unsafe.zip",zip_bytes({"../SKILL.md":CONTENT}))
        with pytest.raises(ValueError):skill_import("large.zip",zip_bytes({"SKILL.md":CONTENT,"resource.txt":"x"*150001}))
        with pytest.raises(ValueError):skill_import("binary.zip",zip_bytes({"SKILL.md":CONTENT,"binary":b"\xff\xff"}))
        with pytest.raises(ValueError):skill_import("duplicates.md",CONTENT.encode())

def test_mcp_credentials_encrypted_and_never_returned_preserved_on_edit(workspace):
    client,(a,b)=workspace
    secret="acceptance-confidential-8d87"
    body={"name":"private-server","url":"http://mcp.example.test/mcp","headers":{"Authorization":"Bearer "+secret},"env":{"SERVICE_KEY":secret}}
    response=client.post("/api/extensions/mcp",headers=headers(a),json=body)
    assert response.status_code==200,response.text
    row=response.json()
    assert secret not in response.text and row["credentialKeys"]["headers"]==["Authorization"]
    with project_scope(a):
        stored=read_record(row["id"],"mcp_config")
        assert secret not in json.dumps(stored)
        assert credentials(stored)["env"]["SERVICE_KEY"]==secret
    changed=client.post("/api/extensions/mcp",headers=headers(a),json={"id":row["id"],"name":"private-server","url":"http://mcp.example.test/new"})
    assert changed.status_code==200
    with project_scope(a):assert credentials(read_record(row["id"]))["headers"]["Authorization"]=="Bearer "+secret
    assert secret not in client.get("/api/extensions",headers=headers(a)).text
    assert client.post("/api/extensions/mcp/"+row["id"]+"/enabled",headers=headers(b),json={"enabled":False}).status_code==404
    assert client.post("/api/extensions/mcp/"+row["id"]+"/delete",headers=headers(b),json={}).status_code==404
    cleared=client.post("/api/extensions/mcp",headers=headers(a),json={"id":row["id"],"name":"private-server","url":"http://mcp.example.test/new","clear_credentials":True})
    assert cleared.json()["credentialKeys"]=={"headers":[],"env":[]}

def test_mcp_import_transport_and_all_validation_before_save(workspace):
    client,(a,b)=workspace
    response=client.post("/api/extensions/mcp/import",headers=headers(a),json={"configuration":{"mcpServers":{
        "remote":{"type":"http","url":"http://mcp.example.test/mcp"},
        "python":{"command":"python","args":["-m","example_server"],"env":{"DEMO_SECRET":"hidden"}}
    }}})
    assert response.status_code==200,response.text
    assert [r["transport"] for r in response.json()]==["streamable-http","stdio"]
    assert "hidden" not in response.text
    bad=client.post("/api/extensions/mcp/import",headers=headers(b),json={"configuration":{"mcpServers":{
        "first":{"url":"http://mcp.example.test/mcp"},"second":{"url":"ftp://invalid"}
    }}})
    assert bad.status_code in {400,422}
    assert not client.get("/api/extensions",headers=headers(b)).json()["servers"]
    cwd=client.post("/api/extensions/mcp/import",headers=headers(b),json={"configuration":{"mcpServers":{"host":{"command":"python","cwd":"/host"}}}})
    assert cwd.status_code==400

@pytest.mark.parametrize("options",[
    {"url":"http://user:password@server/mcp"},
    {"url":"http://server/mcp?api_key=secret"},
    {"url":"http://server/mcp","headers":{"Bad Header":"value"}},
    {"url":"http://server/mcp","headers":{"Authorization":"secret\nInjected: yes"}},
    {"transport":"stdio","command":"python","image":"bad image"},
])
def test_mcp_input_validation(options):
    with pytest.raises(ValueError):MCPInput(name="invalid",**options)

def test_external_tool_aliases_and_scrubbing():
    from aiqa.mcp_client import MCPHub,tool_name
    first={"name":"中文 服务","id":"MCP-123ABC"}
    second={"name":"中文 服务","id":"MCP-456DEF"}
    assert tool_name(first,"same tool")!=tool_name(second,"same tool")
    assert len(tool_name(first,"x"*500))<=64
    hub=MCPHub([])
    hub.secrets=["acceptance-private"]
    assert "acceptance-private" not in hub.scrub("token acceptance-private")

def test_inline_authorization_redacts_raw_token_and_basic_password():
    import base64
    from aiqa.mcp_client import MCPHub
    hub=MCPHub([])
    basic=base64.b64encode(b"test-user:private-basic-password").decode()
    hub._remember_credentials({"Authorization":"bEaReR private-inline-token","Proxy-Authorization":"Basic "+basic})
    output=hub.scrub("private-inline-token / private-basic-password / "+basic)
    assert output=="[REDACTED] / [REDACTED] / [REDACTED]"


def test_parallel_credential_key_creation_is_atomic(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from aiqa.extensions import seal,cipher
    monkeypatch.setattr(settings,"data",tmp_path)
    with ThreadPoolExecutor(max_workers=8) as executor:
        encrypted=list(executor.map(lambda i:seal({"value":i}),range(24)))
    assert [json.loads(cipher().decrypt(value.encode()))["value"] for value in encrypted]==list(range(24))
    assert (tmp_path/"extension-secrets.key").stat().st_mode&0o777==0o600


def test_orphan_mcp_deadline_cleanup(workspace,monkeypatch):
    import time
    import docker
    from aiqa.mcp_client import cleanup_mcp_containers
    removed=[]
    class Container:
        def __init__(self,identity,deadline):self.id=identity;self.labels={"aiqa.mcp.owner":"missing-record","aiqa.mcp.deadline":str(deadline)}
        def remove(self,force=False):removed.append(self.id)
    class Containers:
        def list(self,**kwargs):return [Container("expired",0),Container("healthy",time.time()+300)]
    class Client:
        containers=Containers()
        def close(self):pass
    monkeypatch.setattr(docker,"from_env",lambda:Client())
    cleanup_mcp_containers()
    assert removed==["expired"]
