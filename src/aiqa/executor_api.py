# 本地执行器使用的平台 HTTP 协议适配层。
# Header 中的 X-AIQA-Claim 是本次领取凭证；具体权限检查和数据合并放在 executors.py。

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

class Changes(Strict):
    changes:list[dict]=Field(max_length=100)

@router.get("/executors/identity")
def identity(request:Request):return request.state.user

@router.get("/executors")
def executors():return broker.registry()

@router.post("/executors/register")
def register(body:Register):
    if len(__import__("json").dumps(body.report))>100000:raise ValueError("环境报告过大")
    return broker.register(body.id,body.name,body.report)

@router.post("/executors/claim")
def claim(body:Claim):
    broker.recover()
    return broker.claim(body.executor_id,body.kind)

@router.post("/executors/heartbeat")
def heartbeat(x_aiqa_claim:str=Header()):return broker.heartbeat(x_aiqa_claim)

@router.get("/executors/bundle")
def bundle(x_aiqa_claim:str=Header()):return broker.bundle(x_aiqa_claim)

@router.post("/executors/commit")
def commit(body:Changes,x_aiqa_claim:str=Header()):return broker.commit(x_aiqa_claim,body.changes)

@router.post("/executors/finish")
def finish(x_aiqa_claim:str=Header()):return broker.finish(x_aiqa_claim)

@router.get("/executors/file")
def download(path:str=Query(max_length=1000),x_aiqa_claim:str=Header()):
    broker.authorize_file(x_aiqa_claim,path)
    file=safe_path(path)
    if not file.is_file() or file.stat().st_size>100_000_000:raise HTTPException(404,"找不到文件或超过上限")
    return FileResponse(file,media_type="application/octet-stream")

# 先校验任务是否有权上传此路径，再限制单文件大小。
# 已保存的用例版本不允许用不同内容覆盖；产物先写临时文件再替换，降低读到半文件的风险。
@router.post("/executors/file")
async def upload(request:Request,path:str=Query(max_length=1000),x_aiqa_claim:str=Header()):
    broker.authorize_file(x_aiqa_claim,path,True)
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

@router.get("/models")
def models():return broker.profiles()

@router.get("/models/{identity}")
def model(identity:str):return broker.profile(identity)

@router.post("/models")
def model_save(body:ModelProfile):return save_profile(body)

@router.post("/models/{identity}/delete")
def model_delete(identity:str):
    delete_profile(identity);return {"status":"ok"}


class DeviceLeaseInput(Strict):
    device_id:str
    operation:str
    owner:str=Field(max_length=24,pattern=r"^[A-Za-z0-9_-]+$")
    session_id:str=Field(default="",max_length=80)

@router.post("/executors/device-lease")
def device_lease(body:DeviceLeaseInput,x_aiqa_claim:str=Header()):return broker.device_lease(x_aiqa_claim,**body.model_dump())
