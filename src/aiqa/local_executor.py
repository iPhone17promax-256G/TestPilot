# 本地执行器包含两种运行方式：常驻 gateway 与单份任务的 executor 子进程。
# gateway 处理配对、模型 Key、环境检测和领取任务；run_job 才分派实际 Agent/测试操作。

"""Loopback gateway and outbound-only local job consumer. No shared DB or volume."""
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from .config import settings
from .local_vault import model_vault, private_write, safe_error
from .models import ModelProfile, catalog, request_body

origin=os.environ.get("AIQA_CLIENT_ORIGIN", "http://127.0.0.1:8020").rstrip("/")
bindings_path=settings.client_root/"bindings.json"
pair_code=secrets.token_urlsafe(12)
bindings=json.loads(bindings_path.read_text()) if bindings_path.exists() else {}
if settings.managed_executor:bindings={}
stop=threading.Event()
report={}
processes={}
last_seen={}


def persist_bindings():
    # 自动模式的服务令牌不写账号绑定文件，也不返回浏览器。
    if not settings.managed_executor:private_write(bindings_path,bindings)


# 用已配对账号的令牌访问平台；模型 API Key 不经过这个请求通道。
def platform_request(binding,method,path,**kwargs):
    headers={"Authorization":"Bearer "+binding["platformToken"],"X-AIQA-Project":kwargs.pop("project", "PROJECT")}
    if binding.get("managed"):headers["X-AIQA-Managed-Account"]=binding["accountId"]
    headers.update(kwargs.pop("headers",{}))
    r=httpx.request(method,settings.platform_url.rstrip("/")+path,headers=headers,timeout=30,follow_redirects=False,**kwargs)
    if r.status_code>=400:
        # Return only platform validation errors; never include a local provider key.
        try:message=r.json().get("message", "平台请求失败")
        except ValueError:message="平台请求失败"
        raise ValueError(f"{message}（HTTP {r.status_code}）")
    return r.json()


# 常驻后台线程，每个账号分别维护 agent/test/local 三个进程槽位。
# Agent 经常要等待它创建的测试，如果两者共用单个阻塞槽位，会相互等待。
# 循环负责心跳、取消与回收；subprocess.Popen 启动的是独立进程，不是 Python 协程。
def consumer():
    global report
    from .environment_probe import probe
    report=probe()
    refreshed=time.monotonic()
    cleanup_at=0
    while not stop.wait(2):
        if settings.managed_executor:
            try:
                users=platform_request({"platformToken":settings.managed_token},"GET","/api/internal/accounts")
                active={user["id"] for user in users}
                for user in users:
                    identity=user["id"]
                    if identity not in bindings:
                        # 注册使用确定 ID，重启仍领取原来绑定的任务，不需要在浏览器配对。
                        bindings[identity]=dict(accountId=identity,managed=True,platformToken=settings.managed_token,
                            executorId="EXECUTOR-"+hashlib.sha256(identity.encode()).hexdigest()[:24],role=user["role"])
                        # 迁移旧版已配对账号时沿用执行器 ID，历史对话后续消息仍能被领取。
                        old=json.loads(bindings_path.read_text()).get(identity,{}) if bindings_path.exists() else {}
                        if old.get("executorId"):bindings[identity]["executorId"]=old["executorId"]
                    bindings[identity]["role"]=user["role"]
                for identity in set(bindings)-active:bindings.pop(identity,None)
            except Exception:continue
        if time.monotonic()>cleanup_at:
            cleanup_at=time.monotonic()+15
            try:
                import docker
                client=docker.from_env()
                own={b.get("executorId") for b in bindings.values()}
                for container in client.containers.list(filters={"label":"aiqa.execution=true"}):
                    if container.labels.get("aiqa.executor") in own and float(container.labels.get("aiqa.execution.deadline", "inf"))<time.time():container.remove(force=True)
                client.close()
            except Exception:pass
        if time.monotonic()-refreshed>300:
            report=probe();refreshed=time.monotonic()
        for account,binding in list(bindings.items()):
            try:
                registration=platform_request(binding,"POST","/api/executors/register",json={"id":binding.get("executorId",""),"name":"服务器执行器" if settings.deployment_mode=="server" and settings.managed_executor else "本地执行器","report":report})
                if binding.get("executorId")!=registration["id"]:
                    binding["executorId"]=registration["id"];persist_bindings()
                # 同一账号每种工作各有一个槽位；这是进程级分工，不是给 FastAPI 配 workers 参数。
                for kind in ["agent","test","local"]:
                    slot=(account,kind);current=processes.get(slot)
                    if current:
                        process,job=current
                        headers={"X-AIQA-Claim":job["claim"]}
                        if process.poll() is None:
                            try:
                                state=platform_request(binding,"POST","/api/executors/heartbeat",project=job["projectId"],headers=headers)
                                if state.get("status") in {"cancelled","interrupted"}:process.terminate()
                            except Exception:process.terminate()
                            continue
                        try:platform_request(binding,"POST","/api/executors/finish",project=job["projectId"],headers=headers)
                        except Exception:pass
                        processes.pop(slot,None)
                    alive=[(slot,p) for slot,(p,j) in processes.items() if p.poll() is None]
                    if len(alive)>=settings.max_worker_processes:continue
                    if kind=="agent" and sum(slot[1]=="agent" for slot,p in alive)>=min(settings.max_agent_processes,settings.max_worker_processes-2):continue
                    job=platform_request(binding,"POST","/api/executors/claim",json={"executor_id":binding["executorId"],"kind":kind})
                    if not job:continue
                    root=settings.client_root/"users"/account
                    root.mkdir(parents=True,exist_ok=True,mode=0o700)
                    data=root/"data";data.mkdir(exist_ok=True)
                    # A cache per job avoids concurrent SQL writes; progress is checkpointed separately.
                    cache=root/(job["recordId"]+".sqlite")
                    # 启动子进程时清除继承的全局模型/数据库凭据，再注入本次账号、项目和任务领取信息。
                    env={k:v for k,v in os.environ.items() if k not in {"AIQA_MODEL_KEY","AIQA_DATABASE_URL","AIQA_ACCESS_TOKEN"}}
                    env.update(AIQA_ROLE="executor",AIQA_DATABASE_URL="sqlite:///"+str(cache.resolve()),AIQA_DATA=str(data.resolve()),AIQA_HOST_DATA=str(data.resolve()),
                               AIQA_CLIENT_ROOT=str(settings.client_root.resolve()),AIQA_EXECUTOR_USER=account,AIQA_EXECUTOR_PROJECT=job["projectId"],AIQA_EXECUTOR_CLAIM=job["claim"],
                               AIQA_PLATFORM_URL=settings.platform_url,AIQA_PLATFORM_TOKEN=binding["platformToken"],AIQA_EXECUTOR_ID=binding["executorId"])
                    env["AIQA_PLATFORM_MANAGED"]="true" if binding.get("managed") else "false"
                    # Host-data mount path differs inside the local gateway container.
                    host_root=os.environ.get("AIQA_CLIENT_HOST_ROOT", "")
                    if host_root:env["AIQA_HOST_DATA"]=str(Path(host_root)/"users"/account/"data")
                    process=subprocess.Popen([sys.executable,"-m","aiqa.local_executor","job",json.dumps(job)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    processes[slot]=(process,job)
                last_seen[account]=dict(online=True,seen=time.time())
            except Exception:
                last_seen[account]=dict(online=False,seen=time.time())


# FastAPI 网关启动时创建领取线程；退出时停止线程并终止仍在运行的任务子进程。
@asynccontextmanager
async def lifespan(app):
    thread=threading.Thread(target=consumer,daemon=True);thread.start()
    yield
    stop.set()
    for process,job in processes.values():process.terminate()
    thread.join(timeout=3)

app=FastAPI(title="AIQA Local Executor",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=[origin],allow_methods=["GET","POST"],allow_headers=["Authorization","Content-Type"],allow_private_network=True)

# 本机 HTTP 接口也需要校验来源和配对令牌；监听回环地址不等于所有本机网页都可信。
@app.middleware("http")
async def authorize(request,call_next):
    if request.method=="OPTIONS":return await call_next(request)
    supplied_origin=request.headers.get("origin")
    if supplied_origin and supplied_origin.rstrip("/")!=origin:return __import__("starlette.responses",fromlist=["JSONResponse"]).JSONResponse({"message":"请求来源不匹配"},403)
    if settings.managed_executor:
        from .deployment import managed_authorized
        from fastapi.responses import JSONResponse
        if request.url.path=="/health":return await call_next(request)
        if not managed_authorized(request.headers.get("authorization", "").removeprefix("Bearer ")):return JSONResponse({"message":"内部执行服务拒绝访问"},403)
        account=request.headers.get("x-aiqa-managed-account", "")
        if account not in bindings:return JSONResponse({"message":"执行器尚未注册，请稍后重试"},503)
        if request.url.path=="/pair":return JSONResponse({"message":"自动执行器不需要配对"},409)
        request.state.account=account;request.state.binding=bindings[account]
        return await call_next(request)
    if request.url.path not in {"/health","/pair"}:
        raw=request.headers.get("authorization","").removeprefix("Bearer ")
        account=next((identity for identity,b in bindings.items() if raw and secrets.compare_digest(raw,b["localToken"])),None)
        if not account:return __import__("starlette.responses",fromlist=["JSONResponse"]).JSONResponse({"message":"请重新配对本地执行器"},401)
        request.state.account=account;request.state.binding=bindings[account]
    return await call_next(request)

@app.exception_handler(ValueError)
async def value_error(request,error):
    from fastapi.responses import JSONResponse
    return JSONResponse({"message":str(error)},400)

@app.exception_handler(__import__("fastapi.exceptions",fromlist=["RequestValidationError"]).RequestValidationError)
async def invalid_input(request,error):
    from fastapi.responses import JSONResponse
    return JSONResponse({"message":"本地请求参数格式不正确"},422)

@app.exception_handler(Exception)
async def unexpected(request,error):
    from fastapi.responses import JSONResponse
    return JSONResponse({"message":"本地执行器发生错误，请检查环境和配置"},500)

class Strict(BaseModel):model_config=ConfigDict(extra="forbid")
class Pair(Strict):
    code:str=Field(max_length=200)
    platformToken:str=Field(max_length=500)
    platformUrl:str=Field(max_length=2000)

@app.get("/health")
def health():return {"status":"ok","role":"local-executor","platformOrigin":origin}

# 一次性配对码把当前账号绑定到本地执行器；平台身份由账号令牌核验。
# 成功后轮换配对码，并返回后续访问本地接口所需的 localToken。
@app.post("/pair")
def pair(body:Pair):
    global pair_code
    if not secrets.compare_digest(body.code,pair_code):raise HTTPException(403,"配对码不正确；请查看本地执行器启动输出")
    if body.platformUrl.rstrip("/")!=origin:raise ValueError("平台地址与本地执行器启动配置不匹配")
    binding={"platformToken":body.platformToken,"localToken":secrets.token_urlsafe(32)}
    account=platform_request(binding,"GET","/api/executors/identity")
    if account["id"]=="service":raise ValueError("请使用当前账号的 API Token 配对")
    if any(a==account["id"] and process.poll() is None for (a,k),(process,j) in processes.items()):raise ValueError("本地任务正在执行，请完成后再重新配对")
    prior=bindings.get(account["id"],{});binding["executorId"]=prior.get("executorId","")
    binding["presetInitialized"]=prior.get("presetInitialized",False)
    if prior.get("platformToken") and prior["platformToken"]!=body.platformToken:
        old_id=hashlib.sha256(prior["platformToken"].encode()).hexdigest()
        try:platform_request(binding,"POST",f"/api/auth/tokens/{old_id}/revoke",json={})
        except Exception:pass
    bindings[account["id"]]=binding;private_write(bindings_path,bindings)
    pair_code=secrets.token_urlsafe(12)
    print("AIQA 下一次配对码："+pair_code,flush=True)
    return {"accountId":account["id"],"token":binding["localToken"]}

@app.get("/status")
def status(request:Request):
    b=request.state.binding
    return {"executorId":b.get("executorId",""),"platform":last_seen.get(request.state.account,{}),"report":report,"storageMode":model_vault(request.state.account).storage_mode}

class Probe(Strict):smoke:bool=False
@app.post("/environment")
def environment(body:Probe):
    global report
    from .environment_probe import probe
    report=probe(body.smoke)
    return report


@app.get("/components")
def components_list():
    from .components import list_components
    return list_components()


class ComponentInstall(Strict):
    component:str
    operation:str="install"


@app.post("/components/install")
def component_install(body:ComponentInstall,request:Request):
    if not settings.managed_executor:raise HTTPException(409,"请使用新版自动执行服务")
    from .deployment import require_admin
    require_admin(request.state.binding)
    from .components import install
    return install(body.component,body.operation)


@app.get("/components/{identity}")
def component_logs(identity:str):
    from .components import job_status
    return job_status(identity)

@app.get("/catalog")
def models_catalog():return [m for m in catalog() if m["free"] and m["vision"] and m["tools"] and m["id"]!="openrouter/free"]

# 合并平台的模型元信息和本地 hasKey 状态；返回是否已配置，不返回 Key 的值。
# 首次为该账号添加符合视觉+工具调用条件的 OpenRouter 具体免费模型预设。
@app.get("/models")
def models(request:Request):
    rows=platform_request(request.state.binding,"GET","/api/models")
    found=any(r["provider"]=="openrouter" and r.get("freeOnly") for r in rows)
    if found and not request.state.binding.get("presetInitialized"):
        request.state.binding["presetInitialized"]=True;persist_bindings()
    if not found and not request.state.binding.get("presetInitialized"):
        try:
            candidates=[m for m in catalog() if m["free"] and m["vision"] and m["tools"] and m["id"]!="openrouter/free"]
            candidates.sort(key=lambda m:(not bool(m["reasoningOptions"]), "small" not in m["id"], m["id"]))
            if candidates:
                chosen=candidates[0]
                preset=ModelProfile(name="OpenRouter · 免费视觉模型",model=chosen["id"],default=not rows)
                result=platform_request(request.state.binding,"POST","/api/models",json=preset.public())
                rows.append(result)
                request.state.binding["presetInitialized"]=True;persist_bindings()
        except Exception:pass
    vault=model_vault(request.state.account)
    return [dict(r,hasKey=bool(vault.get(r["id"]))) for r in rows]

class ModelSave(Strict):
    profile:ModelProfile
    apiKey:str=Field(default="",max_length=2048)

# 名称、模型 ID 等元信息发给平台；apiKey 留在本机 LocalVault。
# 更换供应商地址需要重新提供 Key，避免把旧密钥误发到新的地址。
@app.post("/models")
def model_save(body:ModelSave,request:Request):
    account=request.state.account;vault=model_vault(account)
    public=body.profile.public()
    if body.profile.id and not body.apiKey:
        prior=platform_request(request.state.binding,"GET",f"/api/models/{body.profile.id}")
        if prior["baseUrl"].rstrip("/")!=public["baseUrl"].rstrip("/"):raise ValueError("更换模型地址时需要重新填写 API Key")
    result=platform_request(request.state.binding,"POST","/api/models",json=public)
    if body.apiKey:vault.put(result["id"],body.apiKey,result["baseUrl"])
    return dict(result,hasKey=bool(vault.get(result["id"])))

@app.post("/models/{identity}/delete")
def model_delete(identity:str,request:Request):
    platform_request(request.state.binding,"POST",f"/api/models/{identity}/delete")
    model_vault(request.state.account).delete(identity)
    return {"status":"ok"}

# 发送小型图片和工具调用请求验证配置；只是能力冒烟检查，不保证复杂任务一定成功。
# 调用供应商可能消耗额度，异常只返回经过处理的说明，避免原始响应泄漏凭据。
@app.post("/models/{identity}/test")
def model_test(identity:str,request:Request):
    profile=platform_request(request.state.binding,"GET",f"/api/models/{identity}")
    key=model_vault(request.state.account).for_profile(profile)
    if not key:raise ValueError("请先在本地填写 API Key")
    started=time.monotonic()
    try:
        # One small request tests text, declared vision and tool calling together.
        content=[{"type":"text","text":"Read this image if supplied. Call aiqa_probe with ok=true. Do not answer with prose."}]
        if profile.get("vision"):
            content.append({"type":"image_url","image_url":{"url":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJ9sAAAAASUVORK5CYII="}})
        body=dict(model=profile["model"],messages=[{"role":"user","content":content}],max_tokens=1000,tools=[{"type":"function","function":{"name":"aiqa_probe","description":"Confirm response","parameters":{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}}}],**request_body(profile))
        response=httpx.post(profile["baseUrl"].rstrip("/")+"/chat/completions",headers={"Authorization":"Bearer "+key},json=body,timeout=60,follow_redirects=False)
        response.raise_for_status();payload=response.json();calls=payload["choices"][0]["message"].get("tool_calls",[])
        return dict(ok=any(c.get("function",{}).get("name")=="aiqa_probe" for c in calls),model=payload.get("model",profile["model"]),duration=round(time.monotonic()-started,2),visionSent=bool(profile.get("vision")),message="请求成功" if calls else "模型响应成功，但没有按要求调用工具")
    except Exception as error:raise ValueError(safe_error(error)) from None


# 一份领取任务在独立进程内执行，先设项目/账号上下文并初始化本地缓存。
# test 调 execute_run；local 执行设备等操作；agent 先从本地取 Key，再进入模型循环。
# 此进程结束后由 gateway 回收，后续消息可以选择不同模型再启动新的进程。
def run_job(job):
    from .storage import initialize, read_record, update_record
    from .workspace import project_scope, user_scope
    initialize()
    # Never log credentials or raw provider exceptions.
    with project_scope(job["projectId"]),user_scope(settings.executor_user):
        try:
            row=read_record(job["recordId"])
            if job["kind"]=="test":
                from .runners.container import execute_run
                execute_run(job["recordId"])
            elif row["id"].startswith("PROBE-"):
                from .extensions import probe_execute
                probe_execute(row["id"])
            elif row["id"].startswith("ACTION-"):
                from .tools import dispatch
                result=dispatch(row["name"],row["arguments"],environment_id=row.get("environmentId",""))
                update_record(row["id"],lambda p:p.update(status="completed",result=result))
            else:
                from .replica import request
                from .models import activate_local
                profile=request("GET",f"/api/models/{job['modelProfileId']}").json()
                activate_local(profile,job.get("reasoningEffort",""),settings.executor_user)
                from .agent.runtime import execute_task
                execute_task(row["id"])
        except Exception as error:
            try:
                message=str(error) if isinstance(error,ValueError) and "API Key" in str(error) else "本地执行失败，请检查环境或模型配置后继续"
                import traceback
                locations=[dict(file=Path(frame.filename).name,line=frame.lineno,function=frame.name) for frame in traceback.extract_tb(error.__traceback__)[-6:]]
                update_record(job["recordId"],lambda p:p.update(status="error",summary=message,errorType=type(error).__name__,errorLocations=locations) if p["status"] not in {"cancelled","interrupted"} else None)
            except Exception:pass
        finally:
            if job["kind"]=="agent":
                try:
                    from .mobile import close_task_sessions
                    close_task_sessions(job["recordId"])
                except Exception:pass


# 没有 job 参数时启动 8021 本地网关；有 job 参数时只执行这一份工作然后退出。
def main():
    if len(sys.argv)>1 and sys.argv[1]=="job":run_job(json.loads(sys.argv[2]));return
    settings.role="gateway"
    if settings.managed_executor:
        if len(settings.managed_token)<32:raise RuntimeError("内部执行令牌未正确配置")
        print("AIQA 自动执行服务已启动（"+settings.deployment_mode+"）",flush=True)
    else:print("AIQA 本地执行器配对码："+pair_code,flush=True)
    import uvicorn
    uvicorn.run(app,host=os.environ.get("AIQA_CLIENT_BIND","127.0.0.1"),port=8021,log_level="warning",access_log=False)

if __name__=="__main__":main()
