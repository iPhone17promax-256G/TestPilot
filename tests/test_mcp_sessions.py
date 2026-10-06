"""Run these integration checks in agent-worker, where Docker is available."""
import json
import time
from pathlib import Path
import pytest
from sqlalchemy import select
from langchain_core.tools import ToolException
from aiqa.config import settings
from aiqa.extensions import mcp_save,MCPInput
from aiqa.mcp_client import MCPHub
from aiqa.storage import Record,session,uid,read_record,update_record
from aiqa.workspace import project_scope

pytestmark=pytest.mark.skipif(not Path("/var/run/docker.sock").exists(),reason="Real MCP isolation checks require Agent Worker Docker socket")

@pytest.fixture
def workspace():
    # Transport diagnostics are a probe; Agent authorization is covered separately.
    project=uid("PROJECT");task=uid("PROBE")
    with session() as db:
        db.add(Record(id=project,kind="project",payload={"id":project,"name":"MCP session tests"}))
    with project_scope(project),session() as db:
        db.add(Record(id=task,kind="task",status="running",payload={"id":task,"status":"running"}))
    with project_scope(project):
        yield project,task
    with session() as db:
        for row in db.scalars(select(Record).where((Record.project_id==project)|(Record.id==project))):db.delete(row)

@pytest.mark.parametrize("transport",["stdio","streamable-http","sse"])
def test_real_mcp_discovery_validation_errors_and_session_cleanup(workspace,transport):
    import docker
    import httpx
    project,task=workspace;client=docker.from_env();server=None
    try:
        if transport=="stdio":
            body=MCPInput(name="session-demo",transport=transport,command="python",
                args=["/app/examples/extensions/mcp_server.py"],network="none",env={"DEMO_SECRET":"session-acceptance-secret"})
        else:
            name="aiqa-mcp-test-"+uid("SERVER")[-12:].lower()
            server=client.containers.run(settings.runner_image,["python","/app/examples/extensions/mcp_server.py"],
                entrypoint=[],name=name,network=settings.target_network,detach=True,
                environment={"MCP_TRANSPORT":transport},user="1000:1000",read_only=True,
                tmpfs={"/tmp":"rw,nosuid,size=64m"},mem_limit="256m",cap_drop=["ALL"])
            url=f"http://{name}:8765"+("/sse" if transport=="sse" else "/mcp")
            for _ in range(30):
                try:
                    httpx.get(f"http://{name}:8765/",timeout=.5);break
                except httpx.HTTPError:time.sleep(.1)
            body=MCPInput(name="session-demo",transport=transport,url=url)
        saved=mcp_save(body);row=read_record(saved["id"])
        with MCPHub([row],owner=task) as hub:
            assert hub.statuses[row["id"]]["status"]=="connected",hub.statuses
            functions=hub.tools()
            endpoint=next(t for t in functions if "mm_endpoint" in t.name)
            first=endpoint.invoke({"name":"ping"})
            second=endpoint.invoke({"name":"login"})
            assert json.loads(first["text"])["lookupNumber"]==1
            assert json.loads(second["text"])["lookupNumber"]==2
            with pytest.raises(ToolException,match="参数"):endpoint.invoke({"name":"unknown"})
            counter=next(t for t in functions if "session_counter" in t.name)
            assert json.loads(counter.invoke({})["text"])["lookups"]==2
            failure=next(t for t in functions if "demo_error" in t.name)
            with pytest.raises(ToolException,match="demonstration failure"):failure.invoke({})
            assert json.loads(counter.invoke({})["text"])["lookups"]==2 # recover after a tool error
            environment=next(t for t in functions if "environment_check" in t.name)
            result=environment.invoke({})
            assert "session-acceptance-secret" not in json.dumps(result)
            assert not any(json.loads(result["text"])[k] for k in ["modelKeyPresent","databaseUrlPresent","dockerSocketPresent"])
            if transport=="stdio":assert json.loads(result["text"])["suppliedValue"]=="[REDACTED]"
            update_record(row["id"],lambda p:p.update(enabled=False,revision=p["revision"]+1))
            with pytest.raises(ToolException,match="关闭"):counter.invoke({})
        assert not client.containers.list(all=True,filters={"label":"aiqa.mcp.owner="+task})
    finally:
        if server:server.remove(force=True)
        client.close()

def test_failed_connection_does_not_prevent_other_server(workspace):
    project,task=workspace
    good=mcp_save(MCPInput(name="good",transport="stdio",command="python",args=["/app/examples/extensions/mcp_server.py"],network="none"))
    bad=mcp_save(MCPInput(name="bad",url="http://127.0.0.1:9/mcp",timeout=5))
    with MCPHub([read_record(bad["id"]),read_record(good["id"])],owner=task) as hub:
        assert hub.statuses[bad["id"]]["status"]=="error"
        assert hub.statuses[good["id"]]["status"]=="connected"
        assert len(hub.tools())==4
