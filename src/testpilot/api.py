# 平台 HTTP 入口：验证身份、接收参数、保存业务数据和派发任务。
# 平台 API 不运行 Agent 模型循环；Agent 和测试 Job 交给账号绑定的执行服务，本地模式在用户机器，服务端模式在内网服务器。/api/state 返回业务快照供界面展示。

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

log = logging.getLogger("testpilot.api")
WORKER_ONLY_TOOLS = {"workspace_exec", "browser_explore"}


# FastAPI 生命周期钩子：启动时初始化表、演示资料和对外 MCP 会话管理器。
# yield 之前是启动阶段，之后是退出阶段；这里只启动协议服务，不启动 Agent Worker。
# 功能：管理 FastAPI 启停期间的初始化和 MCP 会话管理器。
# 输入：app 是 FastAPI 提供的应用对象；数据库/部署参数取自 settings。
# 输出/影响：yield 期间应用提供服务；启动时建表/种子数据/本地身份并校验内部令牌，退出时释放 MCP 管理器。
@asynccontextmanager
async def lifespan(app):
    initialize()
    t.seed()
    if settings.deployment_mode == "local": accounts.local_identity()
    if settings.managed_executor and len(settings.managed_token) < 32:
        raise RuntimeError("自动执行器需要至少 32 字符的内部令牌，请重新生成安装配置")
    from .job_queue import start_dispatcher
    close_dispatcher=start_dispatcher()
    try:
        from .integrations.mcp import server
        async with server.session_manager.run():
            yield
    finally:close_dispatcher()


app = FastAPI(title="TestPilot", version="0.1.0", lifespan=lifespan)


from . import authentication as accounts
from .workspace import project_scope, user_scope, DEFAULT_PROJECT
from .storage import read_record, list_records


# 每个请求先走此中间件：检查来源、验证 Cookie/Bearer、设置项目和账号上下文。
# HTTP 请求头 X-TestPilot-Project 是选择项目的方式之一；它不代替后端的记录归属检查。
# 同步账号查询交给线程池，避免直接阻塞此 async 中间件的事件循环。
# 功能：在业务路由前校验访问来源、登录和项目身份。
# 输入：request 是 HTTP 请求；call_next 是继续执行下游路由的异步回调。
# 输出/影响：返回下游或拒绝响应；设置 request.state.user 与临时身份上下文，部分响应加缓存/内容类型保护头。
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
        cookie=request.cookies.get("testpilot_session", "")
        service=bool(settings.access_token and secrets.compare_digest(bearer or cookie, settings.access_token))
        from .deployment import managed_authorized
        from starlette.concurrency import run_in_threadpool
        internal=managed_authorized(bearer)
        if path.startswith("/api/internal/"):
            if not internal:return JSONResponse({"message":"内部接口拒绝访问"},status_code=403)
            request.state.user={"id":"internal","username":"执行服务"}
            return await call_next(request)
        if internal:
            user=await run_in_threadpool(accounts.user_by_id,request.headers.get("x-testpilot-managed-account", ""))
            if not user or (settings.deployment_mode == "server" and user["id"] == "LOCAL"):
                return JSONResponse({"message":"内部账号无效"},status_code=403)
        elif settings.deployment_mode == "local":user=await run_in_threadpool(accounts.local_identity)
        else:user=await run_in_threadpool(accounts.session_user,bearer or cookie)
        if not service and not user:
            return JSONResponse({"message":"请先登录 TestPilot"},status_code=401)
        request.state.user=user or {"id":"service","username":"API Token"}
        project=request.headers.get("x-testpilot-project", "") or (request.query_params.get("project_id") if path.startswith(("/api/attachments/", "/api/sources/")) and request.method in {"GET","HEAD"} else "") or DEFAULT_PROJECT
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
# HttpOnly 限制页面脚本读 Cookie；Secure 限制浏览器仅在 HTTPS 请求中携带此凭据。
# 功能：把登录结果整理成响应与会话 Cookie。
# 输入：result 是 login_account 的 (token, user) 二元组，失败为 None。
# 输出/影响：返回含公开用户的 JSONResponse 并设置 HttpOnly Cookie；失败抛 401，不把 token 放在 JSON 内。
def login_response(result):
    if not result:raise HTTPException(401,"账号或密码不正确")
    token,user=result
    response=JSONResponse({"user":user})
    response.set_cookie("testpilot_session",token,httponly=True,secure=True,samesite="strict",max_age=7*86400,path="/")
    return response


# 功能：提供当前登录身份和部署信息给前端。
# 输入：request 提供 Cookie/Bearer；本地模式使用固定身份。
# 输出/影响：返回 user/setupRequired 及部署字段；本地身份不存在时可创建，服务端只查询登录状态。
@app.get("/api/auth/me")
def current_user(request:Request):
    from .deployment import deployment_info
    user=accounts.local_identity() if settings.deployment_mode == "local" else accounts.session_user(request.cookies.get("testpilot_session", "") or request.headers.get("authorization", "").removeprefix("Bearer "))
    return {"user":user,"setupRequired":settings.deployment_mode == "server" and not accounts.has_users(),**deployment_info()}


# 功能：提供受管执行服务需要的账号清单。
# 输入：无参数；内部请求鉴权由 auth 中间件完成。
# 输出/影响：返回公开账号列表；不返回密码或模型 Key。
@app.get("/api/internal/accounts")
def internal_accounts():return accounts.managed_users()


# 功能：让管理员查看可管理的账号。
# 输入：request.state.user 是中间件认证的当前账号。
# 输出/影响：校验管理员后返回公开账号列表，不修改账号。
@app.get("/api/admin/accounts")
def list_accounts(request:Request):
    from .deployment import require_admin
    require_admin(request.state.user)
    return accounts.managed_users()


# 功能：在服务端模式由管理员创建成员账号。
# 输入：body 是用户名/密码表单；request 提供当前管理员身份。
# 输出/影响：返回新账号公开信息；写 users 和密码摘要，拒绝本地模式及重复用户名。
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


# 功能：通过安装初始化接口创建并登录首个管理员。
# 输入：body 是 AccountInput 用户名/密码；只用于服务端首次安装。
# 输出/影响：返回登录响应并设置 Cookie；创建 users/login_sessions，已有管理员时失败。
@app.post("/api/auth/setup")
def setup_account(body:AccountInput):
    if settings.deployment_mode == "local":raise HTTPException(409,"本地模式免登录，无需创建账号")
    accounts.setup_account(body.username,body.password)
    return login_response(accounts.login_account(body.username,body.password))


# 功能：处理服务端模式的账号登录。
# 输入：body 包含用户名和密码；本地模式不走登录流程。
# 输出/影响：返回带会话 Cookie 的公开账号响应，失败 401；验证成功时写登录会话。
@app.post("/api/auth/login")
def account_login(body:AccountInput):
    if settings.deployment_mode == "local":raise HTTPException(409,"本地模式免登录")
    return login_response(accounts.login_account(body.username,body.password))


# 功能：处理浏览器退出登录。
# 输入：request 提供 testpilot_session Cookie。
# 输出/影响：返回成功响应并清 Cookie；数据库撤销对应登录会话。
@app.post("/api/auth/logout")
def account_logout(request:Request):
    accounts.logout_account(request.cookies.get("testpilot_session", ""))
    response=JSONResponse({"status":"ok"});response.delete_cookie("testpilot_session",path="/",secure=True,httponly=True,samesite="strict")
    return response


class PasswordInput(BaseModel):
    current_password:str=Field(min_length=8,max_length=128)
    new_password:str=Field(min_length=8,max_length=128)


# 功能：校验旧密码后更换密码并重新登录。
# 输入：body 含 current_password/new_password；request 提供当前账号。
# 输出/影响：更新 users.password_hash，撤销该账号全部登录会话，再返回新会话 Cookie；服务令牌身份不能改密码。
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


# 功能：为账号页面列出外部访问令牌。
# 输入：request.state.user 提供当前账号 ID。
# 输出/影响：返回该账号令牌元信息列表，不含令牌原文，不写库。
@app.get("/api/auth/tokens")
def tokens_list(request:Request):
    return accounts.tokens_list(request.state.user["id"])


# 功能：为当前账号创建外部访问令牌。
# 输入：body.name 是用途名称；request 提供登录账号，service 身份被拒绝。
# 输出/影响：返回新 token 及元信息；数据库保存摘要，原始 token 只在此结果出现。
@app.post("/api/auth/tokens")
def token_create(body:TokenInput,request:Request):
    if request.state.user["id"]=="service":raise HTTPException(403,"请使用账号登录")
    return accounts.token_create(request.state.user["id"],body.name)


# 功能：通过接口撤销一条账号 API Token。
# 输入：identity 是令牌记录 ID；request 提供当前账号。
# 输出/影响：归属校验后删除令牌并返回 status=ok，不能撤销其他账号的令牌。
@app.post("/api/auth/tokens/{identity}/revoke")
def token_revoke(identity:str,request:Request):
    accounts.token_delete(request.state.user["id"],identity)
    return {"status":"ok"}


class SessionInput(BaseModel):
    token:str=Field(max_length=500)


# 功能：兼容使用安装配置中的平台访问令牌建立 Cookie。
# 输入：body.token 是原始平台访问令牌，须匹配 settings.access_token。
# 输出/影响：返回成功响应并设置 Cookie；不创建普通账号，凭据错误抛 401。
@app.post("/api/session")
def token_login(body:SessionInput):
    if not settings.access_token or not secrets.compare_digest(body.token,settings.access_token):raise HTTPException(401,"平台访问令牌不正确")
    response=JSONResponse({"status":"ok"});response.set_cookie("testpilot_session",settings.access_token,httponly=True,secure=True,samesite="strict",path="/")
    return response


# 功能：把业务 ValueError 映射为 HTTP 错误响应。
# 输入：request/error 由 FastAPI 异常分发传入，error 的文本用于分类。
# 输出/影响：返回 404/409/400 JSONResponse，不修改业务记录；具体状态由缺失资源/幂等冲突等文本决定。
@app.exception_handler(ValueError)
async def value_error(request, error):
    message = str(error)
    status = 404 if any(word in message for word in ["找不到", "不存在"]) else 409 if "幂等键" in message else 400
    return JSONResponse({"message": message}, status_code=status)


# 功能：返回工具参数的 Pydantic 校验失败信息。
# 输入：request/error 由 FastAPI 传入，error 是 ValidationError。
# 输出/影响：返回 HTTP 422 与字段错误列表，排除原始输入/上下文等字段，不写库。
@app.exception_handler(ValidationError)
async def validation_error(request, error):
    return JSONResponse({"message": "工具参数校验失败", "details": error.errors(include_input=False, include_url=False, include_context=False)}, status_code=422)


# 功能：把业务权限拒绝整理成 HTTP 响应。
# 输入：request/error 由 FastAPI 传入，error 是 PermissionError。
# 输出/影响：返回 HTTP 403 与说明，不修改权限状态。
@app.exception_handler(PermissionError)
async def permission_error(request, error):
    return JSONResponse({"message": str(error)}, status_code=403)


# 功能：处理 URL/请求体等 HTTP 参数校验错误。
# 输入：request/error 由 FastAPI 传入，error 是 RequestValidationError。
# 输出/影响：返回 HTTP 422，错误列表排除 input/ctx，避免直接回显这些输入字段。
@app.exception_handler(RequestValidationError)
async def request_validation_error(request,error):
    return JSONResponse({"message":"请求参数缺失或格式不正确","details":[{k:v for k,v in item.items() if k not in {"input","ctx"}} for item in error.errors()]},status_code=422)


# 功能：将保存脚本时的 Python 语法错误告诉调用者。
# 输入：request/error 由 FastAPI 传入；error 提供行号和语法错误描述。
# 输出/影响：返回 HTTP 400，不执行错误脚本，也不写业务记录。
@app.exception_handler(SyntaxError)
async def syntax_error(request, error):
    return JSONResponse({"message": f"Python 语法错误，第 {error.lineno} 行：{error.msg}"}, status_code=400)


# 未知异常对用户返回通用错误，服务端保留堆栈便于排查。
# 这只是异常兜底策略，不能据此推断所有日志和所有测试产物都已全面脱敏。
# 功能：兜底处理没有单独映射的异常。
# 输入：request 提供 URL 路径，error 是未处理异常。
# 输出/影响：日志保存当前异常堆栈，用户收到 HTTP 500 通用说明；不把原始堆栈直接返回页面。
@app.exception_handler(Exception)
async def unexpected_error(request, error):
    log.exception("API error: %s", request.url.path)
    return JSONResponse({"message": "服务器内部错误，请查看服务端日志"}, status_code=500)


# 功能：检查 API 能否连接平台数据库。
# 输入：无参数；使用当前 engine 发 SELECT 1。
# 输出/影响：连接成功返回状态字典，连接失败交给异常处理；只探测连接，不改业务数据。
@app.get("/api/health")
def health():
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    from .job_queue import RedisJobs
    try:RedisJobs().client.ping()
    except Exception:raise HTTPException(503,"执行队列暂不可用") from None
    return {"status": "ok", "database": "mysql", "mode": "live"}


# 返回当前项目的数据快照；ETag 相同时返回 304，减少重复传输。
# 它仍会构建快照来比较摘要，并不是服务端数据库的增量订阅。
# 功能：向页面返回当前项目的业务快照。
# 输入：request.headers 的 if-none-match 可提供上次内容摘要。
# 输出/影响：返回 JSON 或 HTTP 304，带 ETag；调用 state_snapshot 查询数据，不代表数据库增量订阅。
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


# 功能：公开可从平台 HTTP 调用的工具目录。
# 输入：无参数；读取 DEFINITIONS 和仅限 Worker 的工具名单。
# 输出/影响：返回名称/说明/输入 Schema 列表；不执行工具、不包含 Worker 专用工具。
@app.get("/api/tools")
def tool_definitions():
    return [{"name": name, "description": description, "inputSchema": schema.model_json_schema()} for name, (schema, description) in DEFINITIONS.items() if name not in WORKER_ONLY_TOOLS]


# HTTP、CLI、MCP 最终复用同一个 dispatch；路由自身不另实现一套测试逻辑。
# WORKER_ONLY_TOOLS 列出的工作区命令和浏览器探索必须通过 Agent 作业执行；此通用 HTTP 工具接口直接拒绝这些名字，不能在平台 API 请求里运行它们。
# 功能：把 HTTP 工具请求交给统一业务分发器。
# 输入：name 是工具名；body 包含 arguments/task_id/environment_id/key。
# 输出/影响：返回 dispatch 的业务结果并可产生对应写操作；浏览器探索和工作区命令等 Worker 工具在此被拒绝。
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
# 功能：接收手动或外部调用的测试执行请求。
# 输入：body 是 RunStart：用例版本、目标环境、时限和本次配置。
# 输出/影响：调用 run_start 创建 Run/Job，返回 HTTP 202 排队记录；请求线程不运行测试。
@app.post("/api/runs", status_code=202)
def start_run(body: RunStart):
    return t.run_start(**body.model_dump())


# 功能：记录取消测试的意图。
# 输入：run_id 是路径里的运行记录 ID。
# 输出/影响：返回更新后的 Run；执行器轮询后停止容器，本路由不直接杀容器。
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
# 功能：接收新一段 Agent 对话的首条消息。
# 输入：body 是 TaskStart：目标、环境、附件、模型及 Agent 审批模式。
# 输出/影响：创建 Task/agent Job，返回 HTTP 202；Agent 在执行器领取后运行。
@app.post("/api/tasks", status_code=202)
def start_task(body: TaskStart):
    return t.task_start(**body.model_dump())


# 功能：记录停止一段 Agent 对话及其测试的意图。
# 输入：task_id 是当前项目内的对话 ID。
# 输出/影响：返回任务状态，业务层使旧审批失效并取消关联 Run；不在路由内终止操作系统进程。
@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    return t.task_cancel(task_id)


# 功能：请求恢复一段可继续的 Agent 对话。
# 输入：task_id 是待恢复的 Task ID。
# 输出/影响：返回业务层更新结果并重新安排作业；能否恢复由 Task 状态和待批准事项决定。
@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str):
    return t.task_resume(task_id)


class PermissionInput(BaseModel):
    mode: Literal["auto", "readonly", "bypass"]


# 功能：修改对话的 Agent 工具审批模式。
# 输入：task_id 标识对话；body.mode 是 readonly/auto/bypass。
# 输出/影响：返回 set_mode 的 Task 结果，更新权限版本并影响旧审批；不改变容器隔离和账号权限。
@app.post("/api/tasks/{task_id}/permissions")
def task_permissions(task_id: str, body: PermissionInput):
    from .agent.permissions import set_mode
    return set_mode(task_id, body.mode)


class ApprovalInput(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=1000)


# 功能：提交用户对待批准工具调用的决定。
# 输入：task_id/approval_id 标识审批；body 含 approve/reject 和原因。
# 输出/影响：保存审批并按业务规则重新入队；已过期等冲突返回 HTTP 409。
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
# 功能：为已有 Task 追加用户消息。
# 输入：task_id 标识对话；body 含文字、附件引用、采集级别和本轮模型选择。
# 输出/影响：返回 Task 更新结果并安排后续执行；保存消息，不在路由里调用模型。
@app.post("/api/tasks/{task_id}/messages",status_code=202)
def send_message(task_id:str,body:MessageInput):
    return t.task_message(task_id,**body.model_dump())


# async 读取请求流并限制上传大小；文档解析交给线程池中的同步函数。
# async/await 主要让出等待 I/O 的时间，不会把耗时同步解析自动变成异步。
# 功能：以请求流接收对话附件并交给后台线程解析。
# 输入：request 提供原始文件字节；filename 是查询参数，须是允许的文件名。
# 输出/影响：超过 10 MB 返回 413；否则保存附件文件/文本及记录并返回元信息，不把整份文件自动塞给模型。
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


# 功能：分页读取对话附件的解析文本。
# 输入：identity 是附件 ID；offset/limit 是字符偏移与最多读取字符数。
# 输出/影响：归属检查后返回文本分页字典，只读文件和记录。
@app.get("/api/attachments/{identity}/text")
def attachment_text(identity: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=8000, ge=1, le=16000)):
    from .attachments import read_attachment
    return read_attachment(identity, offset, limit)


# 功能：下载当前身份能够访问的对话附件。
# 输入：identity 是附件记录 ID。
# 输出/影响：返回原始文件 FileResponse；文件不存在 404，并设置私有缓存和类型保护头。
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


# 功能：保存被测系统的环境记录。
# 输入：body 含名称、用户访问 URL、执行地址及可选旧环境 ID。
# 输出/影响：返回保存后的环境字典；新增或更新 records，不配置 Python 依赖环境。
@app.post("/api/environments")
def environment_save(body: EnvironmentInput):
    return t.environment_save(**body.model_dump())


class ProjectInput(BaseModel):
    name:str=Field(min_length=1,max_length=120)
    description:str=Field(default="",max_length=2000)
    project_id:str=""


# 功能：新建或更新测试项目。
# 输入：body 含项目名、说明和可选 project_id。
# 输出/影响：返回项目字典；业务层校验创建者权限并写项目记录。
@app.post("/api/projects")
@app.post("/api/project")
def project_save(body:ProjectInput):
    return t.project_save(**body.model_dump())


class ProjectMembers(BaseModel):
    members:list[str]=Field(max_length=100)


# 功能：为项目创建者提供成员编辑目录。
# 输入：identity 是项目 ID；request 提供当前账号。
# 输出/影响：创建者可查看现有成员和候选账号，否则 403；不修改成员关系。
@app.get("/api/projects/{identity}/members")
def project_member_directory(identity:str,request:Request):
    row=read_record(identity,"project")
    if row.get("ownerId") != request.state.user["id"]:raise HTTPException(403,"只有项目创建者可以管理成员")
    return {"members":row.get("members",[]),"accounts":[{"id":user["id"],"username":user["username"]} for user in accounts.managed_users() if user["id"] != request.state.user["id"]]}


# 功能：由项目创建者更新成员列表。
# 输入：identity 是项目 ID；body.members 是账号 ID 列表；request 提供当前账号。
# 输出/影响：核对账号合法性、去重后写项目 payload，返回更新结果。
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
# 功能：校验可视化步骤并预览派生脚本。
# 输入：body.kind/steps 是测试类型与动作列表。
# 输出/影响：返回 code/steps/stepMap；仅编译与校验，不保存版本、不运行测试。
@app.post("/api/cases/preview")
def steps_preview(body: StepsPreview):
    from .steps import compile_steps_details
    from .assets import validate_assets
    steps, code, mapping = compile_steps_details(body.kind, body.steps)
    # 预览源码时复用保存版本所用的语法和大小检查，只返回编译结果；这一步不发送测试请求、不操作浏览器，也不证明用例能够运行通过。
    validate_assets(body.kind, code, "test_case.py", {}, {})
    return {"code": code, "steps": steps, "stepMap": mapping}


class ArchiveInput(BaseModel):
    archived:bool=True


# 功能：调整用例是否归档。
# 输入：case_id 是用例 ID；body.archived 是目标归档状态。
# 输出/影响：更新 records.payload.archived 并返回用例元信息，不删除历史版本或报告。
@app.post("/api/cases/{case_id}/archive")
def case_archive(case_id:str,body:ArchiveInput):
    read_record(case_id,"case")
    return __import__("testpilot.storage",fromlist=["update_record"]).update_record(case_id,lambda p:p.update(archived=body.archived))


class SourceInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=50000)


# 功能：保存项目共享的纯文本资料。
# 输入：body.name/content 是资料名称和文本内容。
# 输出/影响：返回资料记录；业务层写 records，供同项目 Agent 按需读取。
@app.post("/api/sources")
def source_save(body: SourceInput):
    from .sources import save_source
    return save_source(**body.model_dump())


# 功能：列出当前项目的共享资料目录。
# 输入：无显式参数；项目/账号来自请求上下文。
# 输出/影响：返回公开资料元信息列表，不把全部正文一次交给模型。
@app.get("/api/sources")
def sources_list():
    from .sources import sources_list
    return sources_list()


# 功能：上传项目共享文档并解析。
# 输入：request 提供文件字节，filename 是文件名查询参数。
# 输出/影响：限制 10 MB，解析交线程池；保存文件/解析文本/资料记录并返回元信息。
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


# 功能：分页读取项目资料正文。
# 输入：identity 是 source ID；offset/limit 是字符分页参数。
# 输出/影响：返回当前身份可访问的资料文本分页，不修改文件或记录。
@app.get("/api/sources/{identity}/text")
def source_text(identity: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=8000, ge=1, le=16000)):
    from .sources import read_source
    return read_source(identity, offset, limit)


# 功能：下载共享资料原文。
# 输入：identity 是 source ID；资料记录区分上传文件与在线文本。
# 输出/影响：上传资料返回原文件；在线文本生成 TXT 响应，均不改资料。
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


# 功能：归档或恢复一份共享资料。
# 输入：identity 是 source ID；body.archived 是目标状态。
# 输出/影响：更新资料记录并返回公开元信息，不删除已保存原文。
@app.post("/api/sources/{identity}/archive")
def source_archive(identity: str, body: ArchiveInput):
    from .sources import archive_source
    return archive_source(identity, body.archived)


class ModelInput(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    baseUrl: str
    maxSteps: int = Field(ge=5, le=50)


# 功能：保留旧的全局模型设置修改入口。
# 输入：body 含 model/baseUrl/maxSteps；当前 platform 角色直接拒绝使用此路径。
# 输出/影响：兼容路径写 MODEL 记录后返回配置；不保存 Key、不允许只换地址就沿用旧供应商 Key。
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
# 功能：在权限检查后提供测试或浏览器探索证据文件。
# 输入：relative 是 runs/ 或 inspections/ 下的相对路径。
# 输出/影响：返回图片/文本等内联或下载响应；authorize_artifact 检查归属，拒绝任意宿主机文件访问。
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

# 功能：登记或更新 APP 测试设备连接配置。
# 输入：body 含设备名、UDID、Appium/node URL 和可选旧设备 ID。
# 输出/影响：返回设备记录并写配置；保存设备不等于已成功连接手机。
@app.post("/api/devices")
def device_save(body: DeviceInput):
    from .mobile import device_save
    return device_save(**body.model_dump())

# 功能：把设备连通性检查交给执行服务。
# 输入：device_id 是登记设备 ID。
# 输出/影响：返回 local_tool 作业结果，实际检查在执行器进行，平台 HTTP 线程不直接连手机。
@app.post("/api/devices/{device_id}/check")
def device_check(device_id: str):
    from .executors import local_tool
    return local_tool("devices_check", {"id":device_id})

class ApplicationInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    package: str = Field(min_length=1, max_length=200)
    activity: str = ""
    version: str = "installed"

# 功能：登记设备上已有的应用信息。
# 输入：body 含名称、包名、启动 Activity 与版本文字。
# 输出/影响：保存 application 记录并返回元信息；此路由没有上传或安装 APK。
@app.post("/api/applications")
def application_save(body: ApplicationInput):
    from .mobile import app_save
    return app_save(**body.model_dump())

# 功能：接收 APK 并登记应用版本。
# 输入：request 提供 APK 字节，name/package/activity/version 是描述和可选标识。
# 输出/影响：最多 100 MB；调用 app_upload 写包文件和记录，返回应用信息，不在此路由安装到手机。
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
from .knowledge_api import router as knowledge_router
app.include_router(knowledge_router)

from .extension_api import router as extension_router
app.include_router(extension_router)

from .integrations.mcp import server
app.mount("/mcp", server.streamable_http_app())


# 功能：提供构建后的前端静态文件及单页应用入口。
# 输入：path 是请求路径，相对 /app/web/dist 解析。
# 输出/影响：已有静态文件返回 FileResponse；未知 API/MCP 路径 404，其他页面返回 index.html，不改文件。
@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str):
    root = Path("/app/web/dist")
    target = (root / path).resolve()
    if target.is_relative_to(root) and target.is_file():
        return FileResponse(target)
    if path.startswith(("api/", "mcp/")):
        raise HTTPException(404, "接口不存在")
    return FileResponse(root / "index.html")


# 所有认证中间件、路由及挂载应用注册完成后，最后加入 HTTPS 边界，使它在业务认证前执行。
# 原生 TLS 由部署入口提供；不接受客户端通过 X-Forwarded-Proto 把明文请求伪装成 HTTPS。
# 连静态页面、健康检查、首次账号初始化和 MCP 都不设 HTTP 例外，也不自动重发含密码的请求。
from .transport_security import RequireHTTPSMiddleware
app.add_middleware(RequireHTTPSMiddleware)
