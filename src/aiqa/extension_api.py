# Skill/MCP 的 HTTP 管理入口，负责参数适配和调用 extensions.py。
# 打开开关只是让后续 Agent 轮次看到该扩展，不会立即执行 Skill 或调用 MCP 工具。

from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel
from .extensions import (
    SkillInput, MCPInput, extension_snapshot, skill_save, skill_import, skill_public,
    mcp_save, mcp_import, probe_start, delete_extension, stamp,
)
from .storage import read_record, update_record
router=APIRouter(prefix="/api/extensions")

@router.get("")
def snapshot():
    return extension_snapshot()

@router.get("/skills/{identity}")
def skill_detail(identity:str):
    return skill_public(read_record(identity,"skill"),True)

@router.post("/skills")
def save_skill(body:SkillInput):
    return skill_save(body)

@router.post("/skills/import",status_code=201)
async def import_skill(request:Request,filename:str=Query(max_length=180)):
    from starlette.concurrency import run_in_threadpool
    data=bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data)>5_000_000:raise HTTPException(413,"Skill 上传文件最多 5 MB")
    return await run_in_threadpool(skill_import,filename,bytes(data))

class EnabledInput(BaseModel):
    enabled:bool

@router.post("/skills/{identity}/enabled")
def enable_skill(identity:str,body:EnabledInput):
    read_record(identity,"skill")
    row=update_record(identity,lambda p:p.update(enabled=body.enabled,revision=p["revision"]+1,updatedAt=stamp()))
    return skill_public(row)

@router.post("/skills/{identity}/delete")
def remove_skill(identity:str):
    return delete_extension(identity,"skill")

@router.post("/mcp")
def save_mcp(body:MCPInput):
    return mcp_save(body)

class ImportInput(BaseModel):
    configuration:dict

@router.post("/mcp/import")
def import_mcp(body:ImportInput):
    return mcp_import(body.configuration)

@router.post("/mcp/{identity}/enabled")
def enable_mcp(identity:str,body:EnabledInput):
    from .extensions import mcp_public
    read_record(identity,"mcp_config")
    row=update_record(identity,lambda p:p.update(enabled=body.enabled,revision=p["revision"]+1,updatedAt=stamp(),status="untested"))
    return mcp_public(row)

@router.post("/mcp/{identity}/delete")
def remove_mcp(identity:str):
    return delete_extension(identity,"mcp_config")

@router.post("/mcp/{identity}/probe",status_code=202)
def check_mcp(identity:str):
    return probe_start(identity)

@router.get("/probes/{identity}")
def check_result(identity:str):
    return read_record(identity,"mcp_probe")
