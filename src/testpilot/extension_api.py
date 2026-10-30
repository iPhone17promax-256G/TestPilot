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

# 功能：为扩展管理页面提供当前项目的 Skill/MCP 清单。
# 输入：无显式参数；项目由请求上下文确定。
# 输出/影响：返回 extension_snapshot 的公开目录，不连接外部 MCP 服务。
@router.get("")
def snapshot():
    return extension_snapshot()

# 功能：读取一项 Skill 的说明和资源目录。
# 输入：identity 是当前项目 skill ID。
# 输出/影响：返回包含正文的公开 Skill 字典，访问前检查记录归属。
@router.get("/skills/{identity}")
def skill_detail(identity:str):
    return skill_public(read_record(identity,"skill"),True)

# 功能：保存在线编辑的 Skill。
# 输入：body 是 SkillInput，含说明正文与资源文件。
# 输出/影响：SKILL.md 和资源正文保存到 skill 记录的 payload.files，并更新版本元信息；返回公开信息，不在此路由写独立资源文件或运行脚本。
@router.post("/skills")
def save_skill(body:SkillInput):
    return skill_save(body)

# 功能：接收 Markdown/ZIP Skill 上传并导入。
# 输入：request 是文件字节流，filename 指明上传文件名。
# 输出/影响：最多 5 MB，解析/保存在线程池完成，返回新 Skill 信息；上传不等于执行 Skill。
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

# 功能：启用或停用一项 Skill。
# 输入：identity 是 Skill ID；body.enabled 是目标开关。
# 输出/影响：更新 enabled/revision/updatedAt 并返回公开信息；后续 Agent 重新加载时按开关选目录。
@router.post("/skills/{identity}/enabled")
def enable_skill(identity:str,body:EnabledInput):
    read_record(identity,"skill")
    row=update_record(identity,lambda p:p.update(enabled=body.enabled,revision=p["revision"]+1,updatedAt=stamp()))
    return skill_public(row)

# 功能：删除项目中的 Skill 扩展登记。
# 输入：identity 是 Skill ID。
# 输出/影响：调用 delete_extension 核对记录类型和项目权限后删除数据库记录；不检查忙碌任务，也不触发 Skill 脚本。
@router.post("/skills/{identity}/delete")
def remove_skill(identity:str):
    return delete_extension(identity,"skill")

# 功能：保存一项外部 MCP 连接配置。
# 输入：body 是 MCPInput，含传输方式、命令或 URL 与参数。
# 输出/影响：写 MCP 配置记录并返回公开元信息；保存阶段不连接服务。
@router.post("/mcp")
def save_mcp(body:MCPInput):
    return mcp_save(body)

class ImportInput(BaseModel):
    configuration:dict

# 功能：从通用 MCP 配置 JSON 导入多项服务。
# 输入：body.configuration 是 HTTP JSON 已解析的配置字典，通常包含 mcpServers；不是等待再次解析的 JSON 文本。
# 输出/影响：逐项校验并保存 MCP 服务记录，返回导入结果；导入本身不连接工具服务。
@router.post("/mcp/import")
def import_mcp(body:ImportInput):
    return mcp_import(body.configuration)

# 功能：启用/停用 MCP 并使旧探测状态失效。
# 输入：identity 是配置 ID；body.enabled 是目标开关。
# 输出/影响：更新 enabled/revision/updatedAt/status=untested，返回公开配置。
@router.post("/mcp/{identity}/enabled")
def enable_mcp(identity:str,body:EnabledInput):
    from .extensions import mcp_public
    read_record(identity,"mcp_config")
    row=update_record(identity,lambda p:p.update(enabled=body.enabled,revision=p["revision"]+1,updatedAt=stamp(),status="untested"))
    return mcp_public(row)

# 功能：删除项目中的 MCP 配置登记。
# 输入：identity 是 mcp_config ID。
# 输出/影响：核对记录类型和项目权限后删除数据库记录，返回结果；不检查忙碌任务，也不在请求内停止外部服务进程。
@router.post("/mcp/{identity}/delete")
def remove_mcp(identity:str):
    return delete_extension(identity,"mcp_config")

# 功能：提交 MCP 连接探测作业。
# 输入：identity 是待检查配置 ID。
# 输出/影响：创建 mcp_probe/local Job 并返回 HTTP 202，实际连接在执行器执行。
@router.post("/mcp/{identity}/probe",status_code=202)
def check_mcp(identity:str):
    return probe_start(identity)

# 功能：读取 MCP 探测进度和结果。
# 输入：identity 是 mcp_probe 记录 ID。
# 输出/影响：返回已保存的探测字典；只查询，不重复发起连接。
@router.get("/probes/{identity}")
def check_result(identity:str):
    return read_record(identity,"mcp_probe")
