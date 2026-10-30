# 学习导航：consumer 同时对应 Test: Step5 / Agent: Step2；run_job 对应 Test: Step6 / Agent: Step3。
# 执行器学习路线 Executor: Step1～Step7：main 启动服务→consumer 领取→run_job 分派→container.execute_run 准备→launch 创建容器→监控/正常清理→consumer 过期清理。
# 从本文件末尾的 Executor: Step1 [开始] 读起；Step4～Step6 在 src/testpilot/runners/container.py，末尾提示返回这里读 Step7。
# 先沿 kind=test 的普通 API/Web 测试读懂进程与容器，Agent 的模型循环继续看原 Agent 路线；这些编号表示阅读顺序。
# local/server 都使用本文件；常驻服务负责领取与管理，具体工作放在 Popen 启动的独立子进程。
# 本文件既用于本地模式也用于服务端模式：role=gateway 的常驻进程负责领取和管理作业，role=executor 的子进程只执行一份 Job。
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from .config import settings, require_https_url, tls_server_options
from .local_vault import model_vault, private_write, safe_error
from .models import ModelProfile, catalog, request_body

origin=os.environ.get("TESTPILOT_CLIENT_ORIGIN", "https://127.0.0.1:8020").rstrip("/")
bindings_path=settings.client_root/"bindings.json"
pair_code=secrets.token_urlsafe(12)
bindings=json.loads(bindings_path.read_text()) if bindings_path.exists() else {}
if settings.managed_executor:bindings={}
stop=threading.Event()
report={}
processes={}
last_seen={}
platform_client=httpx.Client(timeout=30,follow_redirects=False)


# 功能：保存手动配对执行器的账号绑定关系。
# 输入：无参数；读取进程内 bindings 和安装的 managed_executor 开关。
# 输出/影响：非受管模式写 bindings_path 的私有 JSON；受管模式不写这份绑定文件，返回 None。
def persist_bindings():
    # 这里“自动托管”指 settings.managed_executor 的账号绑定方式，和 Agent 的 auto 工具审批模式无关；托管执行服务使用的内部服务令牌不写绑定文件，也不返回浏览器。
    if not settings.managed_executor:private_write(bindings_path,bindings)


# 用已配对账号的令牌访问平台；模型 API Key 不经过这个请求通道。
# 功能：以绑定账号的身份请求平台 API。
# 输入：binding 提供平台令牌和受管账号；method/path 是 HTTP 方法与平台路径；kwargs 可含 project、请求头、JSON 或文件字节。
# 输出/影响：返回响应 JSON，HTTP 错误抛 ValueError；通过 HTTP 访问平台，不直接连接平台 MySQL。
def platform_request(binding,method,path,**kwargs):
    # 先确认控制面目标使用 TLS，不能把账号/内部令牌发往误配置的 HTTP 地址。
    platform_url=require_https_url(settings.platform_url,"平台通信地址")
    headers={"Authorization":"Bearer "+binding["platformToken"],"X-TestPilot-Project":kwargs.pop("project", "PROJECT")}
    if binding.get("managed"):headers["X-TestPilot-Managed-Account"]=binding["accountId"]
    headers.update(kwargs.pop("headers",{}))
    r=platform_client.request(method,platform_url+path,headers=headers,**kwargs)
    if r.status_code>=400:
        # 平台通信失败时提取响应 JSON 的 message，不回传完整响应；message 是否含敏感信息仍取决于上游，不能据此保证已全面脱敏。
        try:message=r.json().get("message", "平台请求失败")
        except ValueError:message="平台请求失败"
        raise ValueError(f"{message}（HTTP {r.status_code}）")
    return r.json()


# 常驻后台线程，每个账号分别维护 agent/test/local 三个进程槽位。
# Agent 经常要等待它创建的测试，如果两者共用单个阻塞槽位，会相互等待。
# 循环负责心跳、取消与回收；subprocess.Popen 启动的是独立进程，不是 Python 协程。
# Test: Step5 [开始] 找到 kind="test" 的槽位，跟踪 claim 请求与 subprocess.Popen。
# Agent: Step2 [开始] 跟踪同一段代码的 kind="agent" 槽位，理解它与 test 槽位可以同时运行。
# Executor: Step2 [开始] 常驻服务怎样领取 Job、登记槽位并启动独立工作进程。
# 本路线先看下面的账号准备、注册、槽位、HTTP claim 和 Popen；过期容器检查暂时跳过，最后在 Executor: Step7 再读。
# 共享职责（Test: Step5 / Agent: Step2）：常驻领取循环，分别管理每个账号的 agent/test/local 进程槽位。
# 输入：consumer 无函数参数；从 settings、bindings、stop 读取平台地址、账号绑定和服务停止信号。
# 输入来源：HTTP claim 返回 Job 字典，含 kind、recordId、projectId、claim，Agent 还带模型选择。
# 输出：持续运行直到 stop 被设置，正常结束返回 None；实际交付是用 Popen 启动独立任务进程。
# 数据：bindings/processes 是服务内存；业务记录缓存到账号目录的记录ID.sqlite，Job 本身在平台 Redis。
# 数据：该账号文件位于 client_root/users/账号ID/data/；模型 Key 从本机凭据存储取，不放领取消息里。
# 过程：注册→领取→启动→心跳续租→检查取消/退出→回收槽位；Agent 等报告时 test 槽仍可执行。
# 边界：Popen 是多进程，consumer 本身在服务后台线程；不是 FastAPI workers，也不是普通函数传参。
# 这是常驻执行服务里的后台循环，只管理任务进程，不直接进入模型循环。
# 概念：consumer 是 gateway 常驻进程中的领取循环，gateway 的 HTTP 服务启动时会另外启动一个线程运行它。
# “线程”是在同一个 Python 进程里并行做领取管理；下面 Popen 启动的 executor 则是独立进程，各有自己的 settings 和内存。
# 概念：进程槽位是本项目用来登记“某账号的某类工作现在有没有进程在运行”的位置，通过 processes 字典实现。
# slot=(账号ID,作业kind) 是字典键，值 (process,job) 保存进程句柄和它正在处理的工作；空槽位不会提前启动一个空进程。
# 当前 gateway 为每个账号分别管理 agent/test/local 三个槽位，同一槽位已有活进程时暂不领取同类工作。
# agent 处理对话，test 执行测试，local 处理 MCP 检测等辅助作业；local 与 deployment_mode 的本地部署含义不同。
# 例如 Agent 正在等测试报告，占用 agent 槽位，test 槽位仍可启动测试；另一份 Agent 工作要等待 agent 槽位腾出。
# 功能：等待平台的 Redis 队列通知，并独立管理执行子进程。
# 输入：无参数；读取 bindings、进程槽位、settings 和 stop 事件。
# 输出/影响：持续注册/领取/续约并启动或终止子进程，更新本地在线报告；按账号与 agent/test/local 分槽，收到 stop 后退出。
def consumer():
    # 这一块初始化常驻执行服务的检测报告和维护计时。consumer 在 gateway 的后台线程中循环，
    # 空闲槽位通过 HTTP 等待 Redis 通知，监督循环只检查本机进程；Agent/测试仍分别在独立 Python 子进程中运行。
    # report、bindings、processes 是本执行服务的内存状态，不是平台 MySQL 中的业务记录。
    global report
    from .environment_probe import probe
    report=probe()
    refreshed=time.monotonic()
    cleanup_at=0
    registered_at={};heartbeat_at={};accounts_at=0
    # 等待通知与进程维护分离：每个空闲槽最多挂一个等待请求，线程睡在 HTTP/Redis 上，不反复查询 MySQL Job。
    # 唤醒只说明“可能有任务”；资源足够时才 claim，防止先抢到 Job 再因本机满载而拖到租约过期。
    waiters={};ready=set()
    waiter_pool=ThreadPoolExecutor(max_workers=32,thread_name_prefix="queue-wait")
    while not stop.wait(.25):

        # 自动托管执行服务先通过内部 API 获取账号，把账号与代运行的执行器、内部服务令牌对应起来；本地/服务端部署均可启用。
        # binding：就是代码里保存“一名账号对应哪个执行器”的字典。
        # 内部服务令牌：执行服务访问平台内部接口时使用的通行证，让平台确认这是获准的执行服务。它由配置提供，与模型 API Key 用途不同。
        # 已有手动绑定会沿用 executorId，保证旧对话仍能找到执行器；平台已移除的账号不再继续领取。
        # binding 的 role=admin/member 是账号角色，与 settings.role 的进程职责、Agent 的工具审批模式不同。
        # 读取账号失败就等下一轮，不用未确认的名单启动新任务。
        if settings.managed_executor and time.monotonic()>=accounts_at:
            accounts_at=time.monotonic()+15
            try:
                users=platform_request({"platformToken":settings.managed_token},"GET","/api/internal/accounts")
                active={user["id"] for user in users}
                for user in users:
                    identity=user["id"]
                    if identity not in bindings:
                        bindings[identity]=dict(accountId=identity,managed=True,platformToken=settings.managed_token,
                            executorId="EXECUTOR-"+hashlib.sha256(identity.encode()).hexdigest()[:24],role=user["role"])
                        old=json.loads(bindings_path.read_text()).get(identity,{}) if bindings_path.exists() else {}
                        if old.get("executorId"):bindings[identity]["executorId"]=old["executorId"]
                    bindings[identity]["role"]=user["role"]
                for identity in set(bindings)-active:bindings.pop(identity,None)
            except Exception:continue

        # Executor: Step7 [开始] 异常后遗留的执行容器怎样按截止时间清理；先看完 Step5/6 再回这里。
        # 正常测试由 Runner 的 finally 删除容器；如果工作进程意外退出，容器可能还在运行，这一块再次检查。
        # 输入是 Docker 中仍在运行的、带 testpilot.execution=true 标签的容器，以及 bindings 中本服务管理的执行器 IDs。
        # 约每 15 秒检查一次，只有归属标签匹配且 deadline 已过才强制删除；15 秒是检查间隔，不是容器寿命。
        # deadline 由 Step5 的 launch 写成“创建时刻＋ttl＋30秒”；它只是标签，Docker 不会读到时间就自行删除容器。
        # 清理删除的是容器，本次 /work 挂载的宿主运行目录和证据文件仍保留；其他常驻服务不在这组标签筛选内。
        if time.monotonic()>cleanup_at:
            cleanup_at=time.monotonic()+15
            try:
                import docker
                client=docker.from_env()
                own={b.get("executorId") for b in bindings.values()}
                for container in client.containers.list(filters={"label":"testpilot.execution=true"}):
                    if container.labels.get("testpilot.executor") in own and float(container.labels.get("testpilot.execution.deadline", "inf"))<time.time():container.remove(force=True)
                client.close()
            except Exception:pass
        # Executor: Step7 [结束] 执行器路线到此完成：启动/领取→工作进程→临时容器→真实结果→正常或过期清理。
        # 继续学 Agent 可回 Agent: Step3/4；想追结果如何保存到平台，可沿 Test: Step15/16 阅读 HTTP 副本同步。
        # 下方每五分钟刷新的是依赖/镜像检测报告，供平台展示机器能力；它不代表业务用例已经测试通过。
        if time.monotonic()-refreshed>300:
            report=probe();refreshed=time.monotonic()

        # 这一块逐账号向平台注册执行器及环境报告，后续领取使用平台返回的实际 executorId。
        # 每个账号分别管理 agent、test、local 三个工作槽位，避免 Agent 等待报告时堵住测试执行。
        # slot 是内存中的“账号＋工作类型”键，不是 FastAPI 的 workers 参数，也不是数据库表。
        for account,binding in list(bindings.items()):
            try:
                if time.monotonic()>=registered_at.get(account,0):
                    registration=platform_request(binding,"POST","/api/executors/register",json={"id":binding.get("executorId",""),"name":"服务器执行器" if settings.deployment_mode=="server" and settings.managed_executor else "本地执行器","report":report})
                    if binding.get("executorId")!=registration["id"]:
                        binding["executorId"]=registration["id"];persist_bindings()
                    registered_at[account]=time.monotonic()+10
                # 这一块查询当前账号每类工作的槽位：processes.get(slot) 取得已有的 (process,job)，没有登记则为 None。
                # 已有进程还活着就继续续租/检查取消；发现它结束后，下面用 processes.pop 删除登记，让下一份工作使用此槽位。
                # 空槽位还要通过后面的总并发检查才可领取 Job、启动进程，并用 processes[slot]=(process,job) 登记占用。
                for kind in ["agent","test","local"]:
                    slot=(account,kind);current=processes.get(slot)
                    waiting=waiters.get(slot)
                    if waiting and waiting.done():
                        waiters.pop(slot,None)
                        try:
                            if waiting.result().get("available"):ready.add(slot)
                        except Exception:
                            # 网络故障采用短退避；恢复后仍从 Redis 读取积压消息，不把空结果当成已完成。
                            heartbeat_at[(slot,"retry")]=time.monotonic()+3

                    # 槽位已有子进程时，先管理旧工作。进程还活着就向平台 heartbeat 续租，并检查取消/中断；
                    # 续租失败也终止进程，避免失去领取权后继续操作。进程已退出才通知 finish 并释放本地槽位。
                    # 这里使用进程句柄监控退出，Job 中的 claim 则用于证明续租和完成通知针对的是合法领取。
                    if current:
                        process,job=current
                        headers={"X-TestPilot-Claim":job["claim"]}
                        if process.poll() is None:
                            if time.monotonic()>=heartbeat_at.get(slot,0):
                                try:
                                    state=platform_request(binding,"POST","/api/executors/heartbeat",project=job["projectId"],headers=headers)
                                    if state.get("status") in {"cancelled","interrupted"}:process.terminate()
                                except Exception:process.terminate()
                                heartbeat_at[slot]=time.monotonic()+8
                            continue
                        try:platform_request(binding,"POST","/api/executors/finish",project=job["projectId"],headers=headers)
                        except Exception:pass
                        processes.pop(slot,None)

                    if slot not in ready:
                        if slot not in waiters and time.monotonic()>=heartbeat_at.get((slot,"retry"),0):
                            waiters[slot]=waiter_pool.submit(platform_request,dict(binding),"POST","/api/executors/wait",json={"executor_id":binding["executorId"],"kind":kind,"wait_seconds":25})
                        continue

                    # 这一块限制执行器的总并发和 Agent 并发，为测试/辅助工作预留空间，
                    # 然后通过平台 HTTP 接口原子领取相应 kind 的 Job；没有工作就等待下一轮。
                    # 概念：platform_request 是项目封装的 HTTPX 请求，跨进程通信靠 HTTP，不是传 Python db 对象。
                    # claim 是平台给这次领取签发的随机短期凭据，lease 是有效期，初次为当前时间加 45 秒；
                    # 平台用 Redis Lua 原子状态检查防止重复领取，heartbeat 续租；回传还核验账号、项目与当前 dispatchId。
                    # 平台 Token 证明请求身份，claim 证明本份工作的领取权；二者都不是模型供应商 API Key。
                    alive=[(slot,p) for slot,(p,j) in processes.items() if p.poll() is None]
                    if len(alive)>=settings.max_worker_processes:continue
                    if kind=="agent" and sum(slot[1]=="agent" for slot,p in alive)>=min(settings.max_agent_processes,settings.max_worker_processes-2):continue
                    job=platform_request(binding,"POST","/api/executors/claim",json={"executor_id":binding["executorId"],"kind":kind})
                    ready.discard(slot)
                    if not job:continue

                    # 这一块为新工作准备账号私有目录和数据库副本。data 保存该账号的测试输入/产物，
                    # cache 是“记录ID.sqlite”：Agent 用 Task ID，测试用 Run ID，相同对话续聊可复用对应文件。
                    # SQLite 是文件型数据库，这里缓存业务记录；平台 MySQL 保存共享的权威记录，副本通过 HTTP 同步。
                    # Agent 图消息/暂停点另存账号的 checkpoints.sqlite，不能把它和业务副本混为一份数据库。
                    # 这些路径属于 TestPilot 执行服务，不是 MM 等被测项目的源码目录。
                    root=settings.client_root/"users"/account
                    root.mkdir(parents=True,exist_ok=True,mode=0o700)
                    data=root/"data";data.mkdir(exist_ok=True)
                    cache=root/(job["recordId"]+".sqlite")

                    # 这一块组织子进程的操作系统环境变量 env，先移除继承的全局模型 Key、数据库地址和访问令牌，
                    # 再明确传入 executor 进程职责、SQLite 路径、数据目录，以及当前账号/项目/claim/平台 Token。
                    # SSL_CERT_FILE 保留给子进程，继续校验包含本地 CA 与公开 CA 的证书合集，不关闭 TLS 验证。
                    # 子进程启动时用自己的 Settings 读取这些值，不会修改父进程的 settings；父进程仍负责领取和续租。
                    # 子进程通过 HTTP 访问平台，不需要 MySQL 密码；模型 Key 在 Agent: Step3 才从账号凭据库取出。
                    # 托管标记要求请求携带代运行账号，平台还会验证内部令牌，不能只凭一个账号 ID 冒充用户。
                    env={k:v for k,v in os.environ.items() if k not in {"TESTPILOT_MODEL_KEY","TESTPILOT_DATABASE_URL","TESTPILOT_ACCESS_TOKEN","TESTPILOT_REDIS_URL","TESTPILOT_REDIS_PASSWORD"}}
                    env.update(TESTPILOT_ROLE="executor",TESTPILOT_DATABASE_URL="sqlite:///"+str(cache.resolve()),TESTPILOT_DATA=str(data.resolve()),TESTPILOT_HOST_DATA=str(data.resolve()),
                               TESTPILOT_CLIENT_ROOT=str(settings.client_root.resolve()),TESTPILOT_EXECUTOR_USER=account,TESTPILOT_EXECUTOR_PROJECT=job["projectId"],TESTPILOT_EXECUTOR_CLAIM=job["claim"],
                               TESTPILOT_PLATFORM_URL=settings.platform_url,TESTPILOT_PLATFORM_TOKEN=binding["platformToken"],TESTPILOT_EXECUTOR_ID=binding["executorId"])
                    env["TESTPILOT_PLATFORM_MANAGED"]="true" if binding.get("managed") else "false"

                    # 这一块处理 Docker 挂载路径差异：gateway 容器中的 data 路径，
                    # 可能不同于 Docker 守护进程看到的宿主目录，因此单独设置 TESTPILOT_HOST_DATA。
                    # 随后 Popen 用参数数组启动独立 Python 子进程，进入下面的 run_job，不是开协程或拼 shell 命令。
                    # 登记 process 与 Job 后，下轮才能为其续租、取消、检查退出；通信状态保存在 last_seen 供页面显示。
                    host_root=os.environ.get("TESTPILOT_CLIENT_HOST_ROOT", "")
                    if host_root:env["TESTPILOT_HOST_DATA"]=str(Path(host_root)/"users"/account/"data")
                    process=subprocess.Popen([sys.executable,"-m","testpilot.local_executor","job",json.dumps(job)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    processes[slot]=(process,job)
                    heartbeat_at[slot]=0
                last_seen[account]=dict(online=True,seen=time.time())
            except Exception:
                last_seen[account]=dict(online=False,seen=time.time())
    waiter_pool.shutdown(wait=False,cancel_futures=True)
    # Test: Step5 [结束] 下一步：Test: Step6 → 本文件 run_job 的 job["kind"] == "test" 分支。
    # Agent: Step2 [结束] 下一步：Agent: Step3 → 本文件 run_job 的模型配置与 execute_task 分支。
    # Executor: Step2 [结束] 下一步：Executor: Step3 → 本文件 run_job；Popen 的 job 参数会经 main 解析，再传入该函数。


# Executor: Step1 辅助块：main 启动下方 app 后，FastAPI 会调用这个 lifespan，启动一个后台线程运行 consumer。
# yield 前做启动，yield 期间 HTTP 服务与领取线程同时运行；关闭时先通知停止，再终止尚未结束的工作进程。
# 因此常驻服务可以一边响应配置/检测请求，一边领取和管理 Job；真正的工作放在 Step2 创建的子进程中。
# FastAPI 网关启动时创建领取线程；退出时停止线程并终止仍在运行的任务子进程。
# 功能：在执行服务启停时管理 consumer 线程。
# 输入：app 是 FastAPI 生命周期传入的应用对象，本函数未读取它。
# 输出/影响：yield 期间轮询线程运行；关闭时设置 stop、终止子进程并短暂等待线程退出，不返回业务数据。
@asynccontextmanager
async def lifespan(app):
    thread=threading.Thread(target=consumer,daemon=True);thread.start()
    yield
    stop.set()
    for process,job in processes.values():process.terminate()
    thread.join(timeout=3)

app=FastAPI(title="TestPilot Local Executor",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=[origin],allow_methods=["GET","POST"],allow_headers=["Authorization","Content-Type"],allow_private_network=True)

# 本机 HTTP 接口也需要校验来源和配对令牌；监听回环地址不等于所有本机网页都可信。
# 功能：校验执行服务请求的来源与账号绑定。
# 输入：request 提供 Origin/Bearer/内部账号头；call_next 是下游异步路由回调。
# 输出/影响：返回拒绝或业务响应，填充 request.state.account/binding；受管模式验内部令牌，手动模式验本地配对令牌。
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
        account=request.headers.get("x-testpilot-managed-account", "")
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

# 在来源/令牌检查与 CORS 之前先验证真实 TLS 协议；HTTP 连预检和健康接口也不放行。
from .transport_security import RequireHTTPSMiddleware
app.add_middleware(RequireHTTPSMiddleware)

# 功能：把执行服务中的业务输入错误返回页面。
# 输入：request/error 由 FastAPI 异常分发传入，error 是 ValueError。
# 输出/影响：返回 HTTP 400 的 message JSON；不更新任务记录。
@app.exception_handler(ValueError)
async def value_error(request,error):
    from fastapi.responses import JSONResponse
    return JSONResponse({"message":str(error)},400)

# 功能：处理执行服务请求字段格式错误。
# 输入：request/error 由 FastAPI 传入，error 是 RequestValidationError。
# 输出/影响：返回 HTTP 422 通用说明，不回显原始输入，也不写文件。
@app.exception_handler(__import__("fastapi.exceptions",fromlist=["RequestValidationError"]).RequestValidationError)
async def invalid_input(request,error):
    from fastapi.responses import JSONResponse
    return JSONResponse({"message":"本地请求参数格式不正确"},422)

# 功能：兜底处理执行服务的未捕获异常。
# 输入：request/error 由 FastAPI 传入。
# 输出/影响：返回 HTTP 500 通用提示，不在响应中暴露异常原文或密钥。
@app.exception_handler(Exception)
async def unexpected(request,error):
    from fastapi.responses import JSONResponse
    return JSONResponse({"message":"本地执行器发生错误，请检查环境和配置"},500)

class Strict(BaseModel):model_config=ConfigDict(extra="forbid")
class Pair(Strict):
    code:str=Field(max_length=200)
    platformToken:str=Field(max_length=500)
    platformUrl:str=Field(max_length=2000)

# 功能：提供执行服务的轻量健康状态。
# 输入：无参数；读取固定角色标识和平台 origin。
# 输出/影响：返回 status/role/platformOrigin 字典；不检测全部依赖、不查询平台数据库。
@app.get("/health")
def health():return {"status":"ok","role":"local-executor","platformOrigin":origin}

# 一次性配对码把当前账号绑定到本地执行器；平台身份由账号令牌核验。
# 成功后轮换配对码，并返回后续访问本地接口所需的 localToken。
# 功能：使用一次性配对码将账号 API Token 绑定到手动执行器。
# 输入：body 含 code/platformUrl/platformToken；本机启动信息提供当前 pair_code。
# 输出/影响：校验平台身份并写 bindings 私有文件，生成新本地令牌与下一次配对码；返回 accountId/token，账号有运行任务时拒绝重配。
@app.post("/pair")
def pair(body:Pair):
    global pair_code
    require_https_url(body.platformUrl,"配对平台地址")
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
    print("TestPilot 下一次配对码："+pair_code,flush=True)
    return {"accountId":account["id"],"token":binding["localToken"]}

# 功能：显示当前账号执行器的连接及密钥存储方式。
# 输入：request.state.account/binding 来自 authorize；report/last_seen 来自 consumer。
# 输出/影响：返回执行器 ID、平台在线状态、依赖报告和 storageMode；构造密钥库但不返回 Key。
@app.get("/status")
def status(request:Request):
    b=request.state.binding
    return {"executorId":b.get("executorId",""),"platform":last_seen.get(request.state.account,{}),"report":report,"storageMode":model_vault(request.state.account).storage_mode}

class Probe(Strict):smoke:bool=False
# 功能：重新执行依赖与可选浏览器启动自检。
# 输入：body.smoke 决定是否实际启动隔离浏览器做冒烟检查。
# 输出/影响：更新进程内 report 并返回报告；smoke 可创建后清理测试容器，不安装依赖。
@app.post("/environment")
def environment(body:Probe):
    global report
    from .environment_probe import probe
    report=probe(body.smoke)
    return report


# 功能：查询可选 JMeter/Appium 组件安装状态。
# 输入：无参数；依赖 images 中的固定镜像配置。
# 输出/影响：返回组件及最近安装作业，读取 Docker 状态，不启动安装。
@app.get("/components")
def components_list():
    from .components import list_components
    return list_components()


class ComponentInstall(Strict):
    component:str
    operation:str="install"


# 功能：让管理员提交固定组件的安装或启动任务。
# 输入：body.component/operation 指定 JMeter/Appium 和动作；request.state.binding 包含已校验账号角色。
# 输出/影响：仅受管模式且管理员可调用，返回后台安装作业；实际拉镜像/启动容器由 components 线程完成。
@app.post("/components/install")
def component_install(body:ComponentInstall,request:Request):
    if not settings.managed_executor:raise HTTPException(409,"请使用新版自动执行服务")
    from .deployment import require_admin
    require_admin(request.state.binding)
    from .components import install
    return install(body.component,body.operation)


# 功能：读取一份组件操作的进度和日志。
# 输入：identity 是安装作业 ID，来自 install 返回值。
# 输出/影响：返回内存作业快照；服务重启后旧日志不存在，不重复安装组件。
@app.get("/components/{identity}")
def component_logs(identity:str):
    from .components import job_status
    return job_status(identity)

# 功能：向模型设置页列出符合免费预设条件的模型。
# 输入：无参数；读取 OpenRouter 模型目录和缓存。
# 输出/影响：返回同时免费、支持视觉与工具的具体模型列表；不含随机免费路由，不免除供应商 Key。
@app.get("/catalog")
def models_catalog():return [m for m in catalog() if m["free"] and m["vision"] and m["tools"] and m["id"]!="openrouter/free"]

# 合并平台保存的不含密钥的模型配置与执行器密钥存储中的 hasKey 状态；本地/服务端模式均只向页面返回“有无 Key”和存储方式，不返回 Key 正文。
# 首次为该账号添加符合视觉+工具调用条件的 OpenRouter 具体免费模型预设。
# 功能：列出账号模型并在首次使用时尝试建立免费视觉预设。
# 输入：request.state.binding/account 指明平台身份和本机密钥库归属。
# 输出/影响：返回不含密钥的模型配置与 hasKey，可能写免费模型预设和绑定标记；只检查 Key 是否存在，不返回原文。
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


# RAG 密钥与聊天模型分开保存；项目成员使用资料搜索时不会取得 Key 正文。
from .knowledge import KnowledgeConfig
class KnowledgeSave(Strict):
    projectId:str=Field(pattern=r"^[A-Za-z0-9_-]{1,48}$")
    config:KnowledgeConfig
    apiKey:str=Field(default="",max_length=2048)
    reuseModelId:str=Field(default="",max_length=48)


@app.get("/knowledge")
def knowledge_status(request:Request,projectId:str="PROJECT"):
    """先以当前账号向平台核验项目权限，再返回配置/有无密钥，绝不返回凭据原文。"""
    data=platform_request(request.state.binding,"GET","/api/knowledge",project=projectId)
    cfg=data.get("config")
    data["hasKey"]=bool(cfg and model_vault(cfg["ownerId"]).get(cfg["id"]))
    data["storageMode"]=model_vault(request.state.account).storage_mode
    return data


@app.post("/knowledge")
def knowledge_save(body:KnowledgeSave,request:Request):
    """项目授权元信息交给平台；新 Key 或同地址聊天 Key 副本只写执行端凭据库。"""
    public=body.config.public();binding=request.state.binding;account=request.state.account
    before=platform_request(binding,"GET","/api/knowledge",project=body.projectId)
    if not before["canConfigure"]:raise PermissionError("只有项目负责人或管理员可以配置 RAG")
    key=body.apiKey;vault=model_vault(account)
    if body.reuseModelId:
        profile=platform_request(binding,"GET","/api/models/"+body.reuseModelId,project=body.projectId)
        if profile["baseUrl"].rstrip("/")!=public["baseUrl"]:raise ValueError("只能复用相同 Plan 地址的模型 Key")
        key=vault.for_profile(profile)
    prior=before.get("config")
    if not key and prior and prior["ownerId"]==account:key=vault.for_profile(prior)
    if not key:raise ValueError("请填写向量 API Key，或者选择相同 Plan 地址的已有模型密钥")
    result=platform_request(binding,"POST","/api/knowledge/config",project=body.projectId,json=public)
    vault.put(result["id"],key,result["baseUrl"])
    return dict(result,hasKey=True)


class KnowledgeQuery(Strict):
    projectId:str=Field(pattern=r"^[A-Za-z0-9_-]{1,48}$")
    text:str=Field(min_length=1,max_length=2400)


def knowledge_embedding(request,body):
    """平台共享搜索用授权项目的向量配置；项目权限验证后才在私有执行卷读取凭据。"""
    data=platform_request(request.state.binding,"GET","/api/knowledge",project=body.projectId)
    cfg=data.get("config")
    if not cfg:raise ValueError("项目未配置向量模型")
    from .knowledge_vectors import embed
    key=model_vault(cfg["ownerId"]).for_profile(cfg)
    return embed(cfg,key,body.text)


@app.post("/knowledge/test")
def knowledge_test(body:KnowledgeQuery,request:Request):
    result=knowledge_embedding(request,body)
    return dict(status="ok",dimension=result["dimension"],tokens=result["tokens"])


@app.post("/knowledge/embed")
def knowledge_embed(body:KnowledgeQuery,request:Request):
    # 不加入浏览器代理白名单，只供带内部令牌的 API 查询向量，不向浏览器返回高维数组。
    if not request.state.binding.get("managed"):raise HTTPException(403,"仅供受管平台检索")
    return knowledge_embedding(request,body)

# 名称、模型 ID、供应商地址等元信息发给平台；apiKey 交给 model_vault：本地模式使用 LocalVault，服务端自动托管模式使用 ServerVault，不把 Key 写入平台模型配置记录。
# 更换供应商地址需要重新提供 Key，避免把旧密钥误发到新的地址。
# 功能：分别保存不含密钥的模型配置与执行端私有 Key。
# 输入：body.profile 是不含密钥的模型配置，body.apiKey 是可选 Key；request 指明账号和平台绑定。
# 输出/影响：不含密钥的模型配置经 HTTP 写平台，Key 按账号/配置写执行器密钥库并绑定地址；返回配置及 hasKey，不把 Key 返回页面。
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

# 功能：删除账号模型配置及执行器保存的对应 Key。
# 输入：identity 是模型配置 ID；request 指明已验证的账号绑定。
# 输出/影响：经 HTTP 删除平台配置，再删除本机 Key/地址绑定，返回 status=ok；两步不是跨系统原子事务。
@app.post("/models/{identity}/delete")
def model_delete(identity:str,request:Request):
    platform_request(request.state.binding,"POST",f"/api/models/{identity}/delete")
    model_vault(request.state.account).delete(identity)
    return {"status":"ok"}

# 发送小型图片和工具调用请求验证配置；只是能力冒烟检查，不保证复杂任务一定成功。
# 调用供应商可能消耗额度，异常只返回经过处理的说明，避免原始响应泄漏凭据。
# 功能：发送少量模型请求验证可用性和工具调用。
# 输入：identity 是模型配置 ID；request 提供账号，Key 从该账号执行端密钥库加载。
# 输出/影响：返回实际模型、耗时、是否发送图像和工具调用结果；会访问供应商并可能消耗额度，失败消息经 safe_error 处理。
@app.post("/models/{identity}/test")
def model_test(identity:str,request:Request):
    profile=platform_request(request.state.binding,"GET",f"/api/models/{identity}")
    key=model_vault(request.state.account).for_profile(profile)
    if not key:raise ValueError("请先在本地填写 API Key")
    started=time.monotonic()
    try:
        # 向供应商发送小请求；声明支持视觉时附一张像素图，只检查是否返回 testpilot_probe 工具名，不校验图像理解、文本答案或 ok 参数。真实请求可能消耗额度。
        content=[{"type":"text","text":"Read this image if supplied. Call testpilot_probe with ok=true. Do not answer with prose."}]
        if profile.get("vision"):
            content.append({"type":"image_url","image_url":{"url":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJ9sAAAAASUVORK5CYII="}})
        body=dict(model=profile["model"],messages=[{"role":"user","content":content}],max_tokens=1000,tools=[{"type":"function","function":{"name":"testpilot_probe","description":"Confirm response","parameters":{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}}}],**request_body(profile))
        response=httpx.post(profile["baseUrl"].rstrip("/")+"/chat/completions",headers={"Authorization":"Bearer "+key},json=body,timeout=60,follow_redirects=False)
        response.raise_for_status();payload=response.json();calls=payload["choices"][0]["message"].get("tool_calls",[])
        return dict(ok=any(c.get("function",{}).get("name")=="testpilot_probe" for c in calls),model=payload.get("model",profile["model"]),duration=round(time.monotonic()-started,2),visionSent=bool(profile.get("vision")),message="请求成功" if calls else "模型响应成功，但没有按要求调用工具")
    except Exception as error:raise ValueError(safe_error(error)) from None


# 一份领取任务在独立进程内执行，先设项目/账号上下文并初始化本地缓存。
# job.kind=test 时运行测试 Runner，local 时执行设备等辅助操作，agent 时从执行器的账号密钥存储取 Key 后运行 Agent；服务端模式也采用这三个分支。
# 此进程结束后由 gateway 回收，后续消息可以选择不同模型再启动新的进程。
# Test: Step6 [开始] 测试任务子进程入口：建立账号/项目上下文，然后调用 execute_run。
# Agent: Step3 [开始] Agent 任务子进程入口：按本次模型配置加载 Key，再调用 execute_task。
# Executor: Step3 [开始] 在独立 Python 子进程中建立身份，并按 Job.kind 选择工作；本路线只跟 test 分支。
# 输入 Job 由 main 解析命令行 JSON 得到，账号/项目及 SQLite 地址由 Step2 的环境变量传来；结果写记录和文件，而非 return 给浏览器。
# 共享职责（Test: Step6 / Agent: Step3）：在已启动的子进程内建立账号/项目上下文，分派这一份 Job。
# 输入：job 是父进程经命令行 JSON 传来的字典；kind 决定分支，recordId 指向 Task/Run，projectId 限定项目。
# 输入：Agent Job 的 modelProfileId/reasoningEffort 决定本轮模型；领取凭据和账号身份已由父进程注入环境。
# 返回：正常无业务返回值（None），完成后子进程退出；结果通过 update_record 保存，不通过进程返回值传递。
# Test 分支用 execute_run(recordId) 启动隔离测试容器；Agent 分支读取本账号模型配置，从当前执行服务的密钥存储加载 Key，再调用 execute_task。
# 数据：业务操作落入本 Job 的 SQLite 副本并同步平台；Agent 图恢复点另外存，不能把两者混为一份缓存。
# 边界：异常记录到对应 Task/Run，取消状态不被覆盖；finally 清理 Agent 设备会话，父进程随后释放槽位。
# Popen 启动的任务进程入口，job 包含类型、记录 ID、账号/项目关联。
# 功能：执行 gateway 启动的一份独立子进程作业。
# 输入：job 是平台领取返回的 kind/recordId/projectId/模型选择等字典，账号与 claim 来自子进程环境变量。
# 输出/影响：在身份上下文内运行测试、MCP 探测、辅助操作或 Agent；结果写业务副本并回传，Agent 结束清理手机会话。
def run_job(job):
    # 这一块为当前 Job 子进程初始化业务缓存表，并建立账号/项目身份上下文。
    # initialize 作用于父进程指定的 SQLite 副本，不是连接平台 MySQL 直接读写共享表。
    # 概念：project_scope/user_scope 是项目封装的 with 管理器，内部用 Python ContextVar 保存身份；
    # 深层函数通过 current_project/current_user 读取，退出 with 后恢复旧值，避免残留。
    # 身份不会自动跨进程共享，上一 Step 已通过 env 明确传来本 Job 的账号和项目。
    from .storage import initialize, read_record, update_record
    from .workspace import project_scope, user_scope
    initialize()
    with project_scope(job["projectId"]),user_scope(settings.executor_user):
        try:

            # 这一块读取 Job 指向的业务记录，再按工作种类选择执行路线：
            # test 交给 execute_run，准备固定版本并在测试容器运行；PROBE- 交给 MCP 探测，
            # 检查真实连通性；ACTION- 则调用指定工具，把返回值写回辅助操作记录的 result。
            # 这些操作在执行端完成，completed 表示辅助操作完成，不代表测试断言通过。
            row=read_record(job["recordId"])
            # 对测试 Job，recordId 是 RUN ID。execute_run 是在当前子进程内调用的 Runner 函数，接着由它请求 Docker 创建测试容器。
            # 普通 Python 子进程由 Popen 启动，测试容器由 Docker Engine 启动，这里有两层执行边界；不需要先把 Agent 进程也放进这个测试容器。
            if job["kind"]=="test":
                from .runners.container import execute_run
                execute_run(job["recordId"])
            elif row["id"].startswith("PROBE-"):
                from .extensions import probe_execute
                probe_execute(row["id"])
            elif row["id"].startswith("ACTION-"):
                if row["name"]=="_rag_index":
                    from .knowledge_worker import execute
                    execute()
                    return
                from .tools import dispatch
                result=dispatch(row["name"],row["arguments"],environment_id=row.get("environmentId",""))
                update_record(row["id"],lambda p:p.update(status="completed",result=result))
            else:

                # 普通 Task 进入 Agent 路线：先通过 HTTP 读取账号选中的不含密钥的模型配置 profile，
                # 再用 activate_local 从本账号凭据库取 API Key，激活当前子进程的模型参数和思考深度。
                # profile 记录模型 ID、供应商地址、能力等，不含 Key；activate_local 的 local 指“当前执行进程”，
                # 本地部署在用户电脑，服务端部署在内网执行服务，两种模式都使用此激活函数。
                # LocalVault 优先系统凭据库，否则用账号权限受限的明文文件；托管 ServerVault 用 Fernet
                # 加密保存在私有执行卷。Fernet 可解密，主密钥也须保管；实际请求模型时仍要使用原始 Key。
                # 本轮配置只影响当前 Agent 进程，Key 不传给隔离测试容器。
                from .replica import request
                from .models import activate_local
                profile=request("GET",f"/api/models/{job['modelProfileId']}").json()
                activate_local(profile,job.get("reasoningEffort",""),settings.executor_user)

                # 模型准备好后，execute_task 才真正进入多轮 Agent。
                # 图恢复点保存在执行服务当前账号的 checkpoints.sqlite；工具事件等业务内容先写副本，
                # 再同步平台；浏览器探索、创建用例和启动测试等真实工具操作也从执行端发起。
                # 下一步沿 Agent: Step4 阅读模型如何根据工具结果继续决策。
                from .agent.runtime import execute_task
                execute_task(row["id"])

        # 这一块处理准备或执行失败：写 error 状态、摘要，以及最后几层的文件名/行号/函数名，
        # 供页面和排查使用；已经取消或中断的任务不被迟到错误覆盖。
        # 普通异常不直接保存原始正文，但含“API Key”的 ValueError 会保留说明，
        # 因此新增错误信息仍需避免把密钥、claim 或授权头拼进去，不能认定所有异常都已自动脱敏。
        # 如果错误回写也失败，本进程结束后由平台租约与 gateway 检查处理失联。
        except Exception as error:
            try:
                message=str(error) if isinstance(error,ValueError) and ("API Key" in str(error) or locals().get("row",{}).get("name")=="_rag_index") else "本地执行失败，请检查环境或模型配置后继续"
                import traceback
                locations=[dict(file=Path(frame.filename).name,line=frame.lineno,function=frame.name) for frame in traceback.extract_tb(error.__traceback__)[-6:]]
                update_record(job["recordId"],lambda p:p.update(status="error",summary=message,errorType=type(error).__name__,errorLocations=locations) if p["status"] not in {"cancelled","interrupted"} else None)
            except Exception:pass

        # 最后对 Agent 作业，无论成功失败都尝试关闭本 Task 的移动探索会话，释放设备占用。
        # 测试运行中的设备/容器由 Runner 清理；Job 的心跳、finish 和本地进程槽位由上一 Step 的 gateway 管理。
        # 这个函数负责一份 Job 的执行，不负责常驻领取循环。
        finally:
            if job["kind"]=="agent":
                try:
                    from .mobile import close_task_sessions
                    close_task_sessions(job["recordId"])
                except Exception:pass
    # Test: Step6 [结束] 下一步：Test: Step7 → src/testpilot/runners/container.py 的 execute_run 开头。
    # Agent: Step3 [结束] 下一步：Agent: Step4 → src/testpilot/agent/runtime.py 的 execute_task。
    # Executor: Step3 [结束] 下一步：Executor: Step4 → src/testpilot/runners/container.py 的 execute_run 准备区。
    # 调用返回后才会走本函数收尾，最后由 consumer 回收进程；API 进程不运行这些 Agent 循环。


# Executor: Step1 [开始] 执行服务入口：先分清“常驻领取服务”与“只处理一份 Job 的工作进程”。
# 无 job 参数时作为 gateway 启动 HTTP 服务，FastAPI 的 lifespan 辅助块再启动 consumer 线程；有 job 参数时直接进入 run_job。
# gateway 与 executor 是进程职责，local/server 是部署地点；服务端部署也启动同样的领取服务和工作进程。
# 没有 job 参数时启动 8021 本地网关；有 job 参数时只执行这一份工作然后退出。
# 功能：选择常驻执行服务或单作业子进程入口。
# 输入：无显式参数；sys.argv 的 job 参数携带作业 JSON，安装参数来自 settings/环境变量。
# 输出/影响：job 分支执行后返回；常驻分支设 gateway 角色并启动 8021 HTTP 服务，不在一个进程里混跑所有 Agent。
def main():
    # 带 job 参数是任务进程：解析作业，运行一次就返回，不再启动常驻 HTTP 网关。
    if len(sys.argv)>1 and sys.argv[1]=="job":run_job(json.loads(sys.argv[2]));return
    # 没有 job 参数才作为网关，提供配置接口并启动领取任务的后台循环。
    # 启动前验证两端控制面地址与证书，配置不全时不打开不安全的 HTTP 监听。
    require_https_url(settings.platform_url,"平台通信地址")
    require_https_url(origin,"浏览器平台地址")
    tls_options=tls_server_options()
    settings.role="gateway"
    if settings.managed_executor:
        if len(settings.managed_token)<32:raise RuntimeError("内部执行令牌未正确配置")
        print("TestPilot 自动执行服务已启动（"+settings.deployment_mode+"）",flush=True)
    else:print("TestPilot 本地执行器配对码："+pair_code,flush=True)
    import uvicorn
    # 启动执行服务自己的 8021 端口，与平台 API 的 8020 是不同进程和职责。
    uvicorn.run(app,host=os.environ.get("TESTPILOT_CLIENT_BIND","127.0.0.1"),port=8021,log_level="warning",access_log=False,**tls_options)
    # Executor: Step1 [结束] 阅读时先看本文件 lifespan 的短辅助块，再到 Executor: Step2 的 consumer 跟 HTTP 领取和 Popen。
    # uvicorn.run 在服务运行期间会阻塞此调用；它启动应用时已进入 lifespan/consumer，不是等本函数返回后才领取。

if __name__=="__main__":main()
