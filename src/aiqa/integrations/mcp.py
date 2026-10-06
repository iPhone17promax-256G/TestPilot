# AIQA 对外提供的 MCP Server；与 mcp_client.py 接入别人的工具方向相反。
# 函数的英文 docstring 会作为外部工具说明，学习注释放在 # 中，避免无意改变模型看到的 schema/描述。

from aiqa.steps import Step
"""MCP v1 stdio / Streamable HTTP, with business operations behind the HTTP SDK."""
import asyncio
import json
import os
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from .sdk import Client

server = FastMCP("AIQA", host="127.0.0.1", streamable_http_path="/", stateless_http=True, json_response=True)


# 内嵌平台 MCP 已有认证上下文时直接 dispatch；独立 stdio 进程则通过 HTTP SDK 访问平台。
# 两个入口最终复用同一业务实现，不应该复制一份用例保存/执行逻辑。
def call(name, arguments, key="", task_id=""):
    from ..workspace import project_context_var
    if project_context_var.get():
        from ..tools import dispatch
        return dispatch(name,arguments,call_id=key,task_id=task_id)
    client = Client(os.environ.get("AIQA_URL", "http://127.0.0.1:8020"))
    try:
        return client.tool(name, arguments, key=key,task_id=task_id)
    finally:
        client.close()


@server.tool()
async def projects_list() -> list:
    """List project identities. Select via X-AIQA-Project (HTTP) or AIQA_PROJECT_ID (stdio/CLI)."""
    from ..storage import list_records
    from ..workspace import project_context_var
    if project_context_var.get():return list_records("project")
    client=Client()
    try:return (await asyncio.to_thread(client.request,"GET","/api/state"))["projects"]
    finally:client.close()


@server.tool()
async def project_context() -> dict:
    """Read the current project's requirements and target environment."""
    return await asyncio.to_thread(call, "project_context", {})


@server.tool()
async def sources_search(query: str = "", offset: int = 0, limit: int = 12) -> list:
    """Search project material names and parsed text; empty query lists active sources. Page with offset/limit."""
    return await asyncio.to_thread(call, "sources_search", dict(query=query, offset=offset, limit=limit))


@server.tool()
async def sources_read(id: str, offset: int = 0, limit: int = 8000) -> dict:
    """Read a project's uploaded document or text material in bounded pages; unreadable files return a reason."""
    return await asyncio.to_thread(call, "sources_read", dict(id=id, offset=offset, limit=limit))


@server.tool()
async def attachments_read(id: str, offset: int = 0, limit: int = 8000) -> dict:
    """Read an uploaded document by ID within the selected project, in bounded pages."""
    return await asyncio.to_thread(call, "attachments_read", dict(id=id, offset=offset, limit=limit))


@server.tool()
async def cases_list() -> list:
    """List executable cases and their latest revisions."""
    return await asyncio.to_thread(call, "cases_list", {})


@server.tool()
async def cases_read(id: str) -> dict:
    """Read a case and all immutable source versions."""
    return await asyncio.to_thread(call, "cases_read", {"id": id})


@server.tool()
async def steps_capabilities(kind: str) -> dict:
    """Discover visual actions, fields, examples and control blocks; use before authoring steps."""
    return await asyncio.to_thread(call,"steps_capabilities",dict(kind=kind))


# 外部 Agent 可保存 steps 或原生资产；内部 Agent 的可视化优先窄 schema 在 runtime 中单独注册。
@server.tool()
async def cases_save_revision(name: str, kind: str, code: str = "", case_id: str = "", description: str = "", entry: str = "test_case.py", files: dict | None = None, config: dict | None = None, idempotency_key: str = "", steps: list[Step] | None = None) -> dict:
    """Save visual steps (see Step schema) or native pytest/JMX with versioned files/config. Supports JSON queries, advanced locators/waits, and a Python escape-hatch step. Existing case_id appends an immutable version."""
    return await asyncio.to_thread(call, "cases_save_revision", dict(name=name, kind=kind, code=code, case_id=case_id, description=description, entry=entry, files=files or {}, execution_config=config or {}, steps=[s.model_dump() for s in steps] if steps is not None else None), idempotency_key)


@server.tool()
async def runs_start(case_id: str, revision_number: int, environment_id: str = "", timeout: int = 120, overrides: dict | None = None, idempotency_key: str = "", network_capture: str | None = None) -> dict:
    """Queue a real container execution and return its run ID."""
    return await asyncio.to_thread(call, "runs_start", dict(case_id=case_id, revision_number=revision_number, environment_id=environment_id, timeout=timeout, overrides=overrides or {},network_capture=network_capture), idempotency_key)


@server.tool()
async def runs_get(id: str, wait_seconds: int = 0) -> dict:
    """Read a report; optionally wait up to 90 seconds for a terminal result."""
    return await asyncio.to_thread(call, "runs_get", dict(id=id, wait_seconds=wait_seconds))


@server.tool()
async def runs_cancel(id: str) -> dict:
    """Cancel execution while preserving existing evidence."""
    return await asyncio.to_thread(call, "runs_cancel", {"id": id})


@server.tool()
async def artifacts_read(path: str, offset: int = 0, limit: int = 16000) -> list:
    """Read evidence text with pagination; images include base64 content."""
    return evidence(await asyncio.to_thread(call, "artifacts_read", dict(path=path, offset=offset, limit=limit)))


@server.tool()
async def skills_read(name: str, resource: str = "SKILL.md") -> dict:
    """Read a testing Skill; no command is executed automatically."""
    return await asyncio.to_thread(call, "skills_read", dict(name=name, resource=resource))


@server.tool()
async def performance_plan(requests: list[dict]) -> dict:
    """Build native JMX from HTTP request specs; does not save or execute it."""
    return await asyncio.to_thread(call, "performance_plan", {"requests": requests})

@server.tool()
async def applications_list() -> list:
    """List Android application versions and APK hashes."""
    return await asyncio.to_thread(call, "applications_list", {})

@server.tool()
async def devices_list() -> list:
    """List Android devices, Appium endpoints and exclusive occupancy."""
    return await asyncio.to_thread(call, "devices_list", {})

@server.tool()
async def devices_check(id: str) -> dict:
    """Check connectivity to Appium and the configured Android device."""
    return await asyncio.to_thread(call, "devices_check", {"id": id})

@server.tool()
async def mobile_action(action: str, device_id: str = "", app_id: str = "", session_id: str = "", selector: str = "", by: str = "accessibility id", text: str = "", direction: str = "up", reset: str = "reset") -> list:
    """Observe/control Android through open, observe, click, input, swipe, back, close. Close exploration before regression."""
    return evidence(await asyncio.to_thread(call, "mobile_action", dict(action=action,device_id=device_id,app_id=app_id,session_id=session_id,selector=selector,by=by,text=text,direction=direction,reset=reset)))


@server.tool()
async def network_list(session_id: str = "", query: str = "", errors_only: bool = False, offset: int = 0, limit: int = 20) -> dict:
    """List browser capture sessions or paged request summaries in the selected project."""
    return await asyncio.to_thread(call, "network_list", dict(session_id=session_id, query=query, errors_only=errors_only, offset=offset, limit=limit))


@server.tool()
async def network_read(session_id: str, request_id: str, section: str = "metadata", offset: int = 0, limit: int = 8000) -> dict:
    """Read request metadata or bounded request_body/response_body pages; no replay."""
    return await asyncio.to_thread(call, "network_read", dict(session_id=session_id, request_id=request_id, section=section, offset=offset, limit=limit))


@server.tool()
async def network_capture_set(task_id: str, level: str, reason: str) -> dict:
    """Set off/api/detailed for an existing Agent task's current turn and future operations."""
    return await asyncio.to_thread(call, "network_capture_set", dict(level=level, reason=reason), task_id=task_id)


def evidence(result):
    metadata={k:v for k,v in result.items() if k!="image"}
    blocks=[TextContent(type="text",text=json.dumps(metadata,ensure_ascii=False))]
    if "image" in result:blocks.append(ImageContent(type="image",data=result["image"],mimeType=result["mimeType"]))
    return blocks
