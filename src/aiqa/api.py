# 平台 HTTP 入口：验证身份、接收参数、保存业务数据和派发任务。
# 这里不运行 Agent 模型循环；长任务交给本地执行器，/api/state 提供状态快照供界面读取。

import logging
import json
import hashlib
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from .config import settings
from .storage import Record, engine, initialize, session
from . import testing as t
from .tools import DEFINITIONS, dispatch

log = logging.getLogger("aiqa.api")
WORKER_ONLY_TOOLS = {"workspace_exec", "browser_explore"}


# FastAPI 生命周期钩子：启动时初始化表、演示资料和对外 MCP 会话管理器。
# yield 之前是启动阶段，之后是退出阶段；这里只启动协议服务，不启动 Agent Worker。
@asynccontextmanager
async def lifespan(app):
    initialize()
    t.seed()
    if settings.deployment_mode == "local": accounts.local_identity()
    if settings.managed_executor and len(settings.managed_token) < 32:
        raise RuntimeError("自动执行器需要至少 32 字符的内部令牌，请重新生成安装配置")
    from .integrations.mcp import server
    async with server.session_manager.run():
        yield


app = FastAPI(title="AIQA", version="0.1.0", lifespan=lifespan)


from . import authentication as accounts
from .workspace import project_scope, user_scope, DEFAULT_PROJECT
from .storage import read_record, list_records


# 每个请求先走此中间件：检查来源、验证 Cookie/Bearer、设置项目和账号上下文。
# HTTP 请求头 X-AIQA-Project 是选择项目的方式之一；它不代替后端的记录归属检查。
# 同步账号查询交给线程池，避免直接阻塞此 async 中间件的事件循环。
@app.middleware("http")
async def auth(request, call_next):
    path=request.url.path
    # 本地免登录仅供单机回环访问，防止其他网站通过 DNS 重绑定把本机服务当公共 API。
    if settings.deployment_mode == "local" and request.url.hostname not in {"127.0.0.1", "localhost", "::1", "api", "testserver"}:
        return JSONResponse({"message":"本地模式只允许回环地址访问；局域网部署请使用 server 模式"},status_code=403)
    origin=request.headers.get("origin")
    if path.startswith(("/api", "/mcp")) and request.method not in {"GET","HEAD","OPTIONS"} and origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        return JSONResponse({"message":"请求来源不匹配"},status_code=403)
    public={"/api/health","/api/auth/me","/api/auth/setup","/api/auth/login","/api/session"}
    if path.startswith(("/api", "/mcp")) and path not in public:
        bearer=request.headers.get("authorization", "").removeprefix("Bearer ")
        cookie=request.cookies.get("aiqa_session", "")
        service=bool(settings.access_token and secrets.compare_digest(bearer or cookie, settings.access_token))
        from .deployment import managed_authorized
        from starlette.concurrency import run_in_threadpool
        internal=managed_authorized(bearer)
        if path.startswith("/api/internal/"):
            if not internal:return JSONResponse({"message":"内部接口拒绝访问"},status_code=403)
            request.state.user={"id":"internal","username":"执行服务"}
            return await call_next(request)
        if internal:
            user=await run_in_threadpool(accounts.user_by_id,request.headers.get("x-aiqa-managed-account", ""))
            if not user or (settings.deployment_mode == "server" and user["id"] == "LOCAL"):
                return JSONResponse({"message":"内部账号无效"},status_code=403)
        elif settings.deployment_mode == "local":user=await run_in_threadpool(accounts.local_identity)
        else:user=await run_in_threadpool(accounts.session_user,bearer or cookie)
        if not service and not user:
            return JSONResponse({"message":"请先登录 AIQA"},status_code=401)
        request.state.user=user or {"id":"service","username":"API Token"}
        project=request.headers.get("x-aiqa-project", "") or (request.query_params.get("project_id") if path.startswith(("/api/attachments/", "/api/sources/")) and request.method in {"GET","HEAD"} else "") or DEFAULT_PROJECT
        try:
            with user_scope(request.state.user["id"]):read_record(project,"project")
        except ValueError:return JSONResponse({"message":"项目不存在"},status_code=404)
        except PermissionError:return JSONResponse({"message":"你不是该项目的成员"},status_code=403)
        with project_scope(project), user_scope(request.state.user["id"]):response=await call_next(request)
    else:
        response=await call_next(request)
    if path.startswith(("/api", "/mcp")):
        response.headers.setdefault("Cache-Control","private, no-store")
        response.headers.setdefault("X-Content-Type-Options","nosniff")
    return response


class AccountInput(BaseModel):
    username: str = Field(min_length=3,max_length=80,pattern=r"^[A-Za-z0-9_@.\-]+$")
    password: str = Field(min_length=8,max_length=128)


# 把随机会话令牌放进 HttpOnly Cookie；业务返回只含公开账号信息。
# HttpOnly 限制页面脚本读 Cookie，不代表令牌已加密，也不等同于 HTTPS。
def login_response(result):
    if not result:raise HTTPException(401,"账号或密码不正确")
    token,user=result
    response=JSONResponse({"user":user})
    response.set_cookie("aiqa_session",token,httponly=True,samesite="strict",max_age=7*86400,path="/")
    return response


@app.get("/api/auth/me")
def current_user(request:Request):
    from .deployment import deployment_info
    user=accounts.local_identity() if settings.deployment_mode == "local" else accounts.session_user(request.cookies.get("aiqa_session", "") or request.headers.get("authorization", "").removeprefix("Bearer "))
    return {"user":user,"setupRequired":settings.deployment_mode == "server" and not accounts.has_users(),**deployment_info()}


@app.get("/api/internal/accounts")
def internal_accounts():return accounts.managed_users()


@app.get("/api/admin/accounts")
def list_accounts(request:Request):
    from .deployment import require_admin
    require_admin(request.state.user)
    return accounts.managed_users()


@app.post("/api/admin/accounts")
def create_account(body:AccountInput,request:Request):
    from .deployment import require_admin
    from .storage import User,uid
    from sqlalchemy import select
    require_admin(request.state.user)
    if settings.deployment_mode != "server":raise HTTPException(409,"本地模式不需要账号")
    with session() as db:
        if db.scalar(select(User.id).where(User.username == body.username)):raise HTTPException(409,"账号已存在")
        user=User(id=uid("USER"),username=body.username,password_hash=accounts.password_hash(body.password),role="member")
        db.add(user)
        return accounts.public_user(user)


@app.post("/api/auth/setup")
def setup_account(body:AccountInput):
    if settings.deployment_mode == "local":raise HTTPException(409,"本地模式免登录，无需创建账号")
    accounts.setup_account(body.username,body.password)
    return login_response(accounts.login_account(body.username,body.password))


@app.post("/api/auth/login")
def account_login(body:AccountInput):
    if settings.deployment_mode == "local":raise HTTPException(409,"本地模式免登录")
    return login_response(accounts.login_account(body.username,body.password))


@app.post("/api/auth/logout")
def account_logout(request:Request):
    accounts.logout_account(request.cookies.get("aiqa_session", ""))
    response=JSONResponse({"status":"ok"});response.delete_cookie("aiqa_session",path="/")
    return response


class PasswordInput(BaseModel):
    current_password:str=Field(min_length=8,max_length=128)
    new_password:str=Field(min_length=8,max_length=128)


@app.post("/api/auth/password")
def account_password(body:PasswordInput,request:Request):
    if settings.deployment_mode == "local":raise HTTPException(409,"本地模式没有登录密码")
    from .storage import User,LoginSession
    from sqlalchemy import select,delete
    if request.state.user["id"]=="service":raise HTTPException(403,"请使用账号登录后修改密码")
    with session() as db:
        user=db.get(User,request.state.user["id"])
        if not accounts.verify_password(body.current_password,user.password_hash):raise HTTPException(400,"当前密码不正确")
        user.password_hash=accounts.password_hash(body.new_password)
        db.execute(delete(LoginSession).where(LoginSession.user_id==user.id))
    return login_response(accounts.login_account(user.username,body.new_password))


class TokenInput(BaseModel):
    name:str=Field(min_length=1,max_length=80)


@app.get("/api/auth/tokens")
def tokens_list(request:Request):
    return accounts.tokens_list(request.state.user["id"])


@app.post("/api/auth/tokens")
def token_create(body:TokenInput,request:Request):
    if request.state.user["id"]=="service":raise HTTPException(403,"请使用账号登录")
    return accounts.token_create(request.state.user["id"],body.name)


@app.post("/api/auth/tokens/{identity}/revoke")
def token_revoke(identity:str,request:Request):
    accounts.token_delete(request.state.user["id"],identity)
    return {"status":"ok"}


class SessionInput(BaseModel):
    token:str=Field(max_length=500)


@app.post("/api/session")
def token_login(body:SessionInput):
    if not settings.access_token or not secrets.compare_digest(body.token,settings.access_token):raise HTTPException(401,"平台访问令牌不正确")
    response=JSONResponse({"status":"ok"});response.set_cookie("aiqa_session",settings.access_token,httponly=True,samesite="strict",path="/")
    return response


@app.exception_handler(ValueError)
async def value_error(request, error):
    message = str(error)
    status = 404 if any(word in message for word in ["找不到", "不存在"]) else 409 if "幂等键" in message else 400
    return JSONResponse({"message": message}, status_code=status)


@app.exception_handler(ValidationError)
async def validation_error(request, error):
    return JSONResponse({"message": "工具参数校验失败", "details": error.errors(include_input=False, include_url=False, include_context=False)}, status_code=422)


@app.exception_handler(PermissionError)
async def permission_error(request, error):
    return JSONResponse({"message": str(error)}, status_code=403)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request,error):
    return JSONResponse({"message":"请求参数缺失或格式不正确","details":[{k:v for k,v in item.items() if k not in {"input","ctx"}} for item in error.errors()]},status_code=422)


@app.exception_handler(SyntaxError)
async def syntax_error(request, error):
    return JSONResponse({"message": f"Python 语法错误，第 {error.lineno} 行：{error.msg}"}, status_code=400)


# 未知异常对用户返回通用错误，服务端保留堆栈便于排查。
# 这只是异常兜底策略，不能据此推断所有日志和所有测试产物都已全面脱敏。
@app.exception_handler(Exception)
async def unexpected_error(request, error):
    log.exception("API error: %s", request.url.path)
    return JSONResponse({"message": "服务器内部错误，请查看服务端日志"}, status_code=500)


@app.get("/api/health")
def health():
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok", "database": "mysql", "mode": "live"}


# 返回当前项目的数据快照；ETag 相同时返回 304，减少重复传输。
# 它仍会构建快照来比较摘要，并不是服务端数据库的增量订阅。
@app.get("/api/state")
def state(request:Request):
    payload=t.state_snapshot()
    body=json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode()
    etag='"'+hashlib.sha256(body).hexdigest()+'"'
    if request.headers.get("if-none-match")==etag:
        from fastapi.responses import Response
        return Response(status_code=304,headers={"ETag":etag,"Cache-Control":"private, no-cache"})
    from fastapi.responses import Response
    return Response(body,media_type="application/json",headers={"ETag":etag,"Cache-Control":"private, no-cache"})


class ToolRequest(BaseModel):
    arguments: dict = Field(default_factory=dict)
    task_id: str = ""
    environment_id: str = ""
    key: str = Field(default="", max_length=80)


@app.get("/api/tools")
def tool_definitions():
    return [{"name": name, "description": description, "inputSchema": schema.model_json_schema()} for name, (schema, description) in DEFINITIONS.items() if name not in WORKER_ONLY_TOOLS]


# HTTP、CLI、MCP 最终复用同一个 dispatch；路由自身不另实现一套测试逻辑。
# 只允许在 Agent 中运行的工作区命令和浏览器探索，不能在此直接执行。
@app.post("/api/tools/{name}")
def call_tool(name: str, body: ToolRequest):
    if name in WORKER_ONLY_TOOLS:
        raise HTTPException(400, "该工具由独立 Agent Worker 执行，请通过 /api/tasks 提交任务")
    return dispatch(name, body.arguments, task_id=body.task_id, call_id=body.key, environment_id=body.environment_id)


class RunStart(BaseModel):
    case_id: str
    revision_number: int = Field(ge=1)
    environment_id: str
    timeout: int = Field(default=120, ge=5, le=3600)
    key: str = Field(default="", max_length=160)
    overrides: dict = Field(default_factory=dict)
    network_capture: Literal["off", "api", "detailed"] | None = None


# HTTP 202 表示测试已入队，返回 Run ID；是否通过要继续读该 Run 的最终状态。
@app.post("/api/runs", status_code=202)
def start_run(body: RunStart):
    return t.run_start(**body.model_dump())


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    return t.run_cancel(run_id)


class TaskStart(BaseModel):
    goal: str = Field(default="", max_length=6000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)
    network_capture: Literal["off", "api", "detailed"] | None = None
    permission_mode: Literal["auto", "readonly", "bypass"] = "auto"
    environment_id: str
    model_profile_id: str = ""
    reasoning_effort: str = ""
    executor_id: str = ""
    key: str = Field(default="", max_length=160)


# 新建一段可持续追问的 Agent 对话并入队，接口返回不代表模型已经回答完。
@app.post("/api/tasks", status_code=202)
def start_task(body: TaskStart):
    return t.task_start(**body.model_dump())


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    return t.task_cancel(task_id)


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str):
    return t.task_resume(task_id)


class PermissionInput(BaseModel):
    mode: Literal["auto", "readonly", "bypass"]


@app.post("/api/tasks/{task_id}/permissions")
def task_permissions(task_id: str, body: PermissionInput):
    from .agent.permissions import set_mode
    return set_mode(task_id, body.mode)


class ApprovalInput(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=1000)


@app.post("/api/tasks/{task_id}/approvals/{approval_id}", status_code=202)
def task_approval(task_id: str, approval_id: str, body: ApprovalInput):
    from .agent.permissions import decide, ApprovalConflict
    try:
        return decide(task_id, approval_id, body.decision, body.reason)
    except ApprovalConflict as error:
        raise HTTPException(409, str(error)) from None


class MessageInput(BaseModel):
    content:str=Field(default="",max_length=6000)
    attachment_ids:list[str]=Field(default_factory=list,max_length=5)
    network_capture:Literal["off", "api", "detailed"]|None=None
    key:str=Field(default="",max_length=160)
    model_profile_id: str = ""
    reasoning_effort: str = ""


# 追加到同一个 Task，而不是每条消息新建独立任务。
# 消息会记录本次模型与思考深度；执行器在合适的轮次读取，避免中途切换正在调用的模型。
@app.post("/api/tasks/{task_id}/messages",status_code=202)
def send_message(task_id:str,body:MessageInput):
    return t.task_message(task_id,**body.model_dump())


# async 读取请求流并限制上传大小；文档解析交给线程池中的同步函数。
# async/await 主要让出等待 I/O 的时间，不会把耗时同步解析自动变成异步。
@app.post("/api/attachments", status_code=201)
async def attachment_upload(request: Request, filename: str = Query(max_length=180)):
    from .attachments import upload_attachment
    from .documents import MAX_BYTES, validate_name
    from starlette.concurrency import run_in_threadpool
    validate_name(filename)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise HTTPException(413, "单个文件最多 10 MB")
        chunks.append(chunk)
    return await run_in_threadpool(upload_attachment, filename, b"".join(chunks))


@app.get("/api/attachments/{identity}/text")
def attachment_text(identity: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=8000, ge=1, le=16000)):
    from .attachments import read_attachment
    return read_attachment(identity, offset, limit)


@app.get("/api/attachments/{identity}/download")
def attachment_download(identity: str):
    record = read_record(identity, "attachment")
    path = t.safe_path(record["path"])
    if not path.is_file(): raise HTTPException(404, "找不到附件文件")
    return FileResponse(path, filename=record["name"], media_type="application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


class EnvironmentInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str
    execution_url: str = ""
    description: str = Field(default="", max_length=1000)
    environment_id: str = ""


@app.post("/api/environments")
def environment_save(body: EnvironmentInput):
    return t.environment_save(**body.model_dump())


class ProjectInput(BaseModel):
    name:str=Field(min_length=1,max_length=120)
    description:str=Field(default="",max_length=2000)
    project_id:str=""


@app.post("/api/projects")
@app.post("/api/project")
def project_save(body:ProjectInput):
    return t.project_save(**body.model_dump())


class ProjectMembers(BaseModel):
    members:list[str]=Field(max_length=100)


@app.get("/api/projects/{identity}/members")
def project_member_directory(identity:str,request:Request):
    row=read_record(identity,"project")
    if row.get("ownerId") != request.state.user["id"]:raise HTTPException(403,"只有项目创建者可以管理成员")
    return {"members":row.get("members",[]),"accounts":[{"id":user["id"],"username":user["username"]} for user in accounts.managed_users() if user["id"] != request.state.user["id"]]}


@app.post("/api/projects/{identity}/members")
def project_members(identity:str,body:ProjectMembers,request:Request):
    from .storage import update_record
    row=read_record(identity,"project")
    if row.get("ownerId") != request.state.user["id"]:raise HTTPException(403,"只有项目创建者可以修改成员；演示项目为公共项目")
    if any(not accounts.user_by_id(value) or value=="LOCAL" for value in body.members):raise HTTPException(400,"存在无效成员账号")
    return update_record(identity,lambda p:p.update(members=list(dict.fromkeys(body.members))))


class StepsPreview(BaseModel):
    kind: str
    steps: list[dict] = Field(min_length=1, max_length=100)


# 只验证并编译步骤，返回脚本和步骤映射供预览；编译不等于执行测试。
@app.post("/api/cases/preview")
def steps_preview(body: StepsPreview):
    from .steps import compile_steps_details
    from .assets import validate_assets
    steps, code, mapping = compile_steps_details(body.kind, body.steps)
    # Same syntax/size validation as saved revisions; preview never executes code.
    validate_assets(body.kind, code, "test_case.py", {}, {})
    return {"code": code, "steps": steps, "stepMap": mapping}


class ArchiveInput(BaseModel):
    archived:bool=True


@app.post("/api/cases/{case_id}/archive")
def case_archive(case_id:str,body:ArchiveInput):
    read_record(case_id,"case")
    return __import__("aiqa.storage",fromlist=["update_record"]).update_record(case_id,lambda p:p.update(archived=body.archived))


class SourceInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=50000)


@app.post("/api/sources")
def source_save(body: SourceInput):
    from .sources import save_source
    return save_source(**body.model_dump())


@app.get("/api/sources")
def sources_list():
    from .sources import sources_list
    return sources_list()


@app.post("/api/sources/upload", status_code=201)
async def source_upload(request: Request, filename: str = Query(max_length=180)):
    from .sources import upload_source, validate_filename
    from .documents import MAX_BYTES
    from starlette.concurrency import run_in_threadpool
    validate_filename(filename)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise HTTPException(413, "单个文件最多 10 MB")
        chunks.append(chunk)
    return await run_in_threadpool(upload_source, filename, b"".join(chunks))


@app.get("/api/sources/{identity}/text")
def source_text(identity: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=8000, ge=1, le=16000)):
    from .sources import read_source
    return read_source(identity, offset, limit)


@app.get("/api/sources/{identity}/download")
def source_download(identity: str):
    from .sources import file_path
    record = read_record(identity, "source")
    if record.get("storage") == "file":
        return FileResponse(file_path(record), filename=record["name"], media_type="application/octet-stream")
    from starlette.responses import Response
    from urllib.parse import quote
    return Response(record["content"].encode("utf-8"), media_type="application/octet-stream",
                    headers={"Content-Disposition": "attachment; filename*=utf-8''" + quote(record["name"] + ".txt")})


@app.post("/api/sources/{identity}/archive")
def source_archive(identity: str, body: ArchiveInput):
    from .sources import archive_source
    return archive_source(identity, body.archived)


class ModelInput(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    baseUrl: str
    maxSteps: int = Field(ge=5, le=50)


@app.post("/api/settings")
def model_save(body: ModelInput):
    if settings.role == "platform":raise ValueError("全局模型配置已停用，请通过本地执行器保存账号模型配置")
    if body.baseUrl.rstrip("/") != settings.model_url.rstrip("/"):
        raise ValueError("更换模型供应商地址时，请同时在服务端 .env 中配置对应密钥并重启服务")
    with session() as db:
        row = db.get(Record, "MODEL")
        if row:
            row.payload = body.model_dump()
        else:
            db.add(Record(id="MODEL", kind="settings", payload=body.model_dump()))
    return t.model_settings()


# 下载证据前验证其所属项目/执行目录，避免只拼接文件路径就开放任意文件。
@app.get("/api/artifacts/{relative:path}")
def artifact(relative: str):
    if not relative.startswith(("runs/", "inspections/")):
        raise HTTPException(404, "找不到产物")
    from .access import authorize_artifact
    path = authorize_artifact(relative)
    if not path.is_file():
        raise HTTPException(404, "找不到产物")
    inline = {".png": "image/png", ".json": "application/json", ".txt": "text/plain", ".log": "text/plain", ".py": "text/plain", ".md": "text/plain"}
    if path.suffix.lower() in inline:
        return FileResponse(path, media_type=inline[path.suffix.lower()], headers={"X-Content-Type-Options": "nosniff"})
    return FileResponse(path, media_type="application/octet-stream", filename=path.name, headers={"X-Content-Type-Options": "nosniff"})


class DeviceInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    udid: str = Field(min_length=1, max_length=120)
    appium_url: str
    node_url: str = ""
    device_id: str = ""

@app.post("/api/devices")
def device_save(body: DeviceInput):
    from .mobile import device_save
    return device_save(**body.model_dump())

@app.post("/api/devices/{device_id}/check")
def device_check(device_id: str):
    from .executors import local_tool
    return local_tool("devices_check", {"id":device_id})

class ApplicationInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    package: str = Field(min_length=1, max_length=200)
    activity: str = ""
    version: str = "installed"

@app.post("/api/applications")
def application_save(body: ApplicationInput):
    from .mobile import app_save
    return app_save(**body.model_dump())

@app.post("/api/applications/upload")
async def application_upload(request: Request, name: str = "Android APK", package: str = "", activity: str = "", version: str = ""):
    from .mobile import app_upload
    chunks=[]; size=0
    async for chunk in request.stream():
        size+=len(chunk)
        if size>100_000_000:raise HTTPException(413,"APK 最大 100MB")
        chunks.append(chunk)
    return app_upload(b"".join(chunks),name,package,activity,version)


from .executor_api import router as executor_router
app.include_router(executor_router)
from .runtime_api import router as runtime_router
app.include_router(runtime_router)

from .extension_api import router as extension_router
app.include_router(extension_router)

from .integrations.mcp import server
app.mount("/mcp", server.streamable_http_app())


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str):
    root = Path("/app/web/dist")
    target = (root / path).resolve()
    if target.is_relative_to(root) and target.is_file():
        return FileResponse(target)
    if path.startswith(("api/", "mcp/")):
        raise HTTPException(404, "接口不存在")
    return FileResponse(root / "index.html")
