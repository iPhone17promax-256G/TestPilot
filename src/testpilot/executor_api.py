# 执行器访问平台所用的 HTTP 路由层；执行器可以在本机或内网服务器，协议和领取凭据检查相同，具体业务委托给 executors.py。
# Header 中的 X-TestPilot-Claim 是本次领取凭证；具体权限检查和数据合并放在 executors.py。

from fastapi import APIRouter, Request, HTTPException, Header, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from . import executors as broker
from .models import ModelProfile, save_profile, delete_profile
from .testing import safe_path

router=APIRouter(prefix="/api")

class Strict(BaseModel):
    model_config=ConfigDict(extra="forbid")

class Register(Strict):
    id:str=Field(default="", max_length=48, pattern=r"^[A-Za-z0-9_-]*$")
    name:str=Field(min_length=1,max_length=120)
    report:dict=Field(default_factory=dict)

class Claim(Strict):
    executor_id:str
    kind:str
    wait_seconds:float=Field(default=0,ge=0,le=25)

class Changes(Strict):
    changes:list[dict]=Field(max_length=100)

# 功能：让执行器确认当前平台认证身份。
# 输入：request.state.user 由平台中间件填入。
# 输出/影响：返回公开账号字典，不生成模型凭据。
@router.get("/executors/identity")
def identity(request:Request):return request.state.user

# 功能：列出当前账号登记的执行器。
# 输入：无显式参数；账号上下文由平台认证提供。
# 输出/影响：返回 broker.registry 的执行器列表，不启动子进程。
@router.get("/executors")
def executors():return broker.registry()

# 功能：登记执行器及其依赖检查报告。
# 输入：body.id/name/report 来自执行服务，report 的 JSON 文本不得超过上限。
# 输出/影响：返回执行器记录；broker 更新 records 中的注册及在线状态。
@router.post("/executors/register")
def register(body:Register):
    if len(__import__("json").dumps(body.report))>100000:raise ValueError("环境报告过大")
    return broker.register(body.id,body.name,body.report)

# 功能：为执行器领取一份指定类型作业。
# 输入：body.executor_id/kind 标识执行器与 agent/test/local 作业类型。
# 输出/影响：从 Redis 原子领取，返回领取数据或空结果；过期作业由独立维护线程回收。
@router.post("/executors/claim")
def claim(body:Claim):
    return broker.claim(body.executor_id,body.kind,body.wait_seconds)


@router.post("/executors/wait")
async def wait(body:Claim):
    # Redis 的异步等待不占 MySQL 事务，也不占 FastAPI 的同步线程池。
    from starlette.concurrency import run_in_threadpool
    from .job_queue import wait_available
    from .workspace import current_user
    registered=await run_in_threadpool(broker.registry)
    if body.executor_id not in {r["id"] for r in registered}:raise HTTPException(403,"执行器未绑定当前账号")
    if body.kind not in {"agent","test","local"}:raise HTTPException(422,"任务类型不正确")
    available=await wait_available(current_user(),body.executor_id,body.kind,body.wait_seconds or 25)
    return {"available":available}

# 功能：续期正在执行的作业领取凭证。
# 输入：x_testpilot_claim 来自请求头，是 Job 领取令牌而不是模型 Key。
# 输出/影响：返回租约结果；broker 核对账号与期限后更新作业状态。
@router.post("/executors/heartbeat")
def heartbeat(x_testpilot_claim:str=Header()):return broker.heartbeat(x_testpilot_claim)

# 功能：下载当前作业授权的数据快照。
# 输入：x_testpilot_claim 是请求头中的领取令牌。
# 输出/影响：返回授权业务快照供执行器建立 SQLite 副本；按需包含 MCP 执行凭据 localCredentials，不包含模型供应商 API Key。
@router.get("/executors/bundle")
def bundle(x_testpilot_claim:str=Header()):return broker.bundle(x_testpilot_claim)

# 功能：接收执行器业务副本的增量更新。
# 输入：body.changes 是差异列表，x_testpilot_claim 是领取凭证。
# 输出/影响：broker 校验字段/归属并合并至平台数据库；返回同步结果，和 Git commit 无关。
@router.post("/executors/commit")
def commit(body:Changes,x_testpilot_claim:str=Header()):return broker.commit(x_testpilot_claim,body.changes)

# 功能：确认执行器已完成本份作业。
# 输入：x_testpilot_claim 是领取凭证。
# 输出/影响：更新 Job 的调度状态并返回结果；完成 Job 不等于测试断言通过。
@router.post("/executors/finish")
def finish(x_testpilot_claim:str=Header()):return broker.finish(x_testpilot_claim)

# 功能：按作业权限下载执行所需文件。
# 输入：path 是数据目录相对路径；x_testpilot_claim 用于核对文件是否属于本作业。
# 输出/影响：文件存在且不超过 100 MB 时返回二进制响应，否则 404；不改文件。
@router.get("/executors/file")
def download(path:str=Query(max_length=1000),x_testpilot_claim:str=Header()):
    broker.authorize_file(x_testpilot_claim,path)
    file=safe_path(path)
    if not file.is_file() or file.stat().st_size>100_000_000:raise HTTPException(404,"找不到文件或超过上限")
    return FileResponse(file,media_type="application/octet-stream")

# 先校验任务是否有权上传此路径，再限制单文件大小。
# 已保存的用例版本不允许用不同内容覆盖；产物先写临时文件再替换，降低读到半文件的风险。
# 功能：接收执行器回传的产物文件。
# 输入：request 提供字节流，path 是授权目标路径，x_testpilot_claim 是领取令牌。
# 输出/影响：限制 100 MB，临时文件写完后替换目标；旧用例版本不同内容禁止覆盖，返回包含文件大小的 JSON 字典 {"bytes": size}。
@router.post("/executors/file")
async def upload(request:Request,path:str=Query(max_length=1000),x_testpilot_claim:str=Header()):
    broker.authorize_file(x_testpilot_claim,path,True)
    file=safe_path(path)
    chunks=[];size=0
    async for chunk in request.stream():
        size+=len(chunk)
        if size>100_000_000:raise HTTPException(413,"单个执行产物最大 100 MB")
        chunks.append(chunk)
    data=b"".join(chunks)
    if path.startswith("cases/") and file.exists() and file.read_bytes()!=data:
        raise HTTPException(409,"已经保存的用例版本文件不可覆盖")
    file.parent.mkdir(parents=True,exist_ok=True)
    temp=file.with_name(file.name+".upload-"+__import__("uuid").uuid4().hex)
    temp.write_bytes(data);temp.replace(file)
    return {"bytes":size}

# 功能：列出当前账号的不含密钥的模型配置。
# 输入：无参数；账号上下文由中间件提供。
# 输出/影响：返回配置列表，不返回执行器密钥库的 Key。
@router.get("/models")
def models():return broker.profiles()

# 功能：获取一项账号模型配置。
# 输入：identity 是 model_profile 记录 ID。
# 输出/影响：归属检查后返回不含密钥的模型配置，不加载模型 Key。
@router.get("/models/{identity}")
def model(identity:str):return broker.profile(identity)

# 功能：保存账号的一项不含密钥的模型配置。
# 输入：body 是 ModelProfile，含供应商地址、模型标识和能力参数。
# 输出/影响：写 model_profile 记录并返回不含密钥的模型配置；密钥存储由执行服务负责。
@router.post("/models")
def model_save(body:ModelProfile):return save_profile(body)

# 功能：删除当前账号的一项不含密钥的模型配置。
# 输入：identity 是配置 ID。
# 输出/影响：调用 delete_profile 删除平台记录，返回 status=ok；此接口不直接访问执行器密钥库。
@router.post("/models/{identity}/delete")
def model_delete(identity:str):
    delete_profile(identity);return {"status":"ok"}


class DeviceLeaseInput(Strict):
    device_id:str
    operation:str
    owner:str=Field(max_length=24,pattern=r"^[A-Za-z0-9_-]+$")
    session_id:str=Field(default="",max_length=80)

# 功能：提供设备独占租约的申请/续期/释放接口。
# 输入：body 含设备及动作参数；x_testpilot_claim 绑定当前已领取作业，可以是 APP 回归 Run 或设备探索操作。
# 输出/影响：broker 校验 claim 与租约占用者，申请/续期/释放 device_leases 并返回结果；设备是平台登记资源，此函数不校验其项目成员归属。
@router.post("/executors/device-lease")
def device_lease(body:DeviceLeaseInput,x_testpilot_claim:str=Header()):return broker.device_lease(x_testpilot_claim,**body.model_dump())
