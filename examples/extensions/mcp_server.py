"""Small real MCP server for learning. Not a mock of AIQA's tool dispatcher."""
import os
from typing import Literal
from mcp.server.fastmcp import FastMCP
mcp=FastMCP("AIQA extension demo",host="0.0.0.0",port=8765)
lookups=0

@mcp.tool()
def mm_endpoint(name:Literal["ping","login"]) -> dict:
    """Return a Mattermost API example. Call separately for each endpoint."""
    global lookups
    lookups+=1
    examples={
        "ping":{"method":"GET","path":"/api/v4/system/ping","expected_status":200,"expected_json":{"status":"OK"}},
        "login":{"method":"POST","path":"/api/v4/users/login","body":{"login_id":"aiqa-nonexistent","password":"invalid-password"},"expected_status":401},
    }
    return dict(name=name,lookupNumber=lookups,**examples[name])

@mcp.tool()
def session_counter() -> dict:
    """Return this server process's lookup count, demonstrating a persistent session."""
    return {"lookups":lookups}

@mcp.tool()
def demo_error() -> str:
    """Deliberately fail, to demonstrate a visible tool error."""
    raise ValueError("demonstration failure")

@mcp.tool()
def environment_check() -> dict:
    """Verify this demo process did not inherit AIQA model/database credentials."""
    return {"modelKeyPresent":bool(os.getenv("AIQA_MODEL_KEY")),"databaseUrlPresent":bool(os.getenv("AIQA_DATABASE_URL")),"dockerSocketPresent":os.path.exists("/var/run/docker.sock"),"suppliedValue":os.getenv("DEMO_SECRET","")}

if __name__=="__main__":
    mcp.run(transport=os.getenv("MCP_TRANSPORT","stdio"))
