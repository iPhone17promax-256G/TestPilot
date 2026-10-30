# 学习导航：Test: Step16 的 commit 是平台端接收执行器结果；不是 Git 的 commit。
# 平台作业派发接口：账号绑定的执行器通过 HTTP 领取 Job、续租、取业务快照、回传差异；执行器可在用户本机，也可在服务端模式的内网配套服务。
# 它管理的是执行权和数据归属，既不调用模型，也不在平台启动测试容器。

"""Platform job broker. No model calls, browsers or execution occur here."""
import copy
import secrets
from datetime import timedelta
from pathlib import PurePosixPath
from sqlalchemy import select
from .storage import Record, session, now, uid, read_record, list_records
from .job_queue import RedisJobs, enqueue, complete, get_job
from .workspace import current_project, current_user

PRIVATE_KINDS = {"executor", "model_profile"}
EXECUTOR_WRITABLE = {"mcp_config","case", "run", "task", "network_session", "mobile_session", "mobile-session", "device", "mcp_probe", "local_action"}
IMMUTABLE = {"accountId","ownerId", "executorId", "projectId", "modelProfileId", "environmentId", "createdAt", "id", "dispatchId"}


# 功能：检查记录的账号归属。
# 输入：row 是 Record；owner 是可选指定账号，省略时使用 current_user。
# 输出/影响：返回 ownerId 是否匹配的布尔值；只检查内存对象，不查询或修改数据库。
def owned(row, owner=None):
    return row.payload.get("ownerId") == (owner or current_user())


# 功能：核对网络证据会话及父任务/运行的账号权限。
# 输入：row 是 network_session 记录，db 是当前事务 Session。
# 输出/影响：返回布尔值；查询关联 Task/Run 并校验 accountId/ownerId，不改记录。
def network_owned(row, db):
    payload=row.payload
    if payload.get("accountId") and payload["accountId"]!=current_user():return False
    parent=db.get(Record,payload.get("ownerId") if payload.get("kind")=="run" else payload.get("taskId"))
    return bool(parent and (not parent.payload.get("ownerId") or owned(parent)))


# 功能：列出当前账号的不含密钥的模型配置。
# 输入：无参数；账号来自 current_user。
# 输出/影响：查询 records(kind=model_profile) 并返回 payload 列表，不读取密钥库。
def profiles():
    with session() as db:
        return [r.payload for r in db.scalars(select(Record).where(Record.kind=="model_profile")) if owned(r)]


# 功能：读取当前账号的一项模型配置。
# 输入：identity 是 model_profile ID。
# 输出/影响：返回复制的不含密钥的模型配置字典；记录类型或账号不匹配就抛异常，不返回 API Key。
def profile(identity):
    with session() as db:
        r=db.get(Record,identity)
        if not r or r.kind!="model_profile" or not owned(r):raise ValueError("当前账号中找不到模型配置")
        return dict(r.payload)


# 功能：根据执行器最近注册时间估算在线状态。
# 输入：payload 是执行器元信息，lastSeen 是 UTC 时间字符串。
# 输出/影响：25 秒内返回 True，缺失或格式不合法 False；不发送连通性请求。
def online(payload):
    from datetime import datetime
    try:return (now()-datetime.fromisoformat(payload["lastSeen"])).total_seconds()<25
    except (KeyError,ValueError):return False


# 功能：列出当前账号登记的执行器及在线标记。
# 输入：无参数；账号来自当前身份上下文。
# 输出/影响：返回公开执行器列表，online 由最近注册时间计算；只查询数据库。
def registry():
    with session() as db:
        return [dict(r.payload,online=online(r.payload)) for r in db.scalars(select(Record).where(Record.kind=="executor")) if owned(r)]


# 功能：创建或刷新账号执行器登记。
# 输入：identity 是已有 ID 或空串，name 是展示名，report 是依赖检查报告。
# 输出/影响：写 records(kind=executor) 的 ownerId/lastSeen/report，返回元信息；不启动测试。
def register(identity,name,report):
    with session() as db:
        r=db.get(Record,identity) if identity else None
        if r and (r.kind!="executor" or not owned(r)):raise PermissionError("执行器归属不匹配")
        payload=dict(id=identity or uid("EXECUTOR"),ownerId=current_user(),name=name[:120],lastSeen=now().isoformat(),report=report)
        if r:r.payload=payload
        else:db.add(Record(id=payload["id"],kind="executor",payload=payload))
        return payload


# 功能：选择指定或默认执行器。
# 输入：identity 可选；候选只来自当前账号 registry。
# 输出/影响：返回执行器 ID 或空串；指定 ID 必须属于账号，默认优先在线项，指定项并不额外保证在线。
def selected_executor(identity=""):
    rows=registry()
    if identity:
        item=next((r for r in rows if r["id"]==identity),None)
        if not item:raise ValueError("当前账号中找不到本地执行器")
        return item["id"]
    return next((r["id"] for r in rows if r["online"]),rows[0]["id"] if rows else "")


# Redis Streams 消费者组读取待处理 Job，Lua 一次检查 queued 并签发 claim；不查询 MySQL jobs 表。
# 只分配归属当前账号、符合执行器绑定的工作；生成短期 claim 凭证，后续请求都要带它。
# 功能：原子领取属于账号和执行器的一份作业。
# 输入：executor_id 是登记 ID，kind 是 agent/test/local；当前账号来自认证上下文。
# 输出/影响：返回 Job/业务记录/项目/claim/模型选择或 None；Redis 原子领取并设置租约，MySQL 锁住业务记录核验当前 dispatchId。
def claim(executor_id,kind,wait_seconds=0):
    if kind not in {"agent","test","local"}:raise ValueError("任务类型不正确")
    if executor_id not in {r["id"] for r in registry()}:raise PermissionError("执行器未绑定当前账号")
    # 在 Redis 阻塞等待时不持有 MySQL 连接或行锁；只有真正读到 Job 才打开业务事务。
    queue=RedisJobs()
    deadline=__import__("time").monotonic()+wait_seconds
    while True:
        remaining=max(0,deadline-__import__("time").monotonic()) if wait_seconds else 0
        job=queue.claim(current_user(),executor_id,kind,remaining)
        if not job:return None
        try:
            with session() as db:
                row=db.scalar(select(Record).where(Record.id==job.record_id).with_for_update())
                if not row or not owned(row) or row.payload.get("executorId") not in {"",None,executor_id}:
                    complete(db,job,"cancelled")
                    continue
                # dispatchId 是这次调度的编号；旧消息即使还在 Stream 中，也不能重复启动已更换轮次的对话。
                if row.status=="cancelled" or row.payload.get("dispatchId")!=job.id:
                    complete(db,job,"cancelled")
                    continue
                if row.status!="queued":
                    # Redis 重启后若恢复出旧排队消息，业务记录可能已经完成或产生副作用，不能再次执行。
                    if row.status=="running":
                        row.status="interrupted";row.payload=dict(row.payload,status="interrupted",summary="调度状态不一致，请确认实际结果后继续")
                    complete(db,job,"interrupted" if row.status=="interrupted" else "finished")
                    continue
                row.status="running"
                row.payload=dict(row.payload,status="running",executorId=executor_id,summary="执行器正在执行")
                selected={}
                if row.kind=="task":
                    pending=[m for m in row.payload.get("messages",[]) if m.get("role")=="user" and not m.get("suppressed")]
                    message=next((m for m in pending if not m.get("delivered")),pending[-1] if pending else {})
                    selected=dict(modelProfileId=message.get("modelProfileId",row.payload.get("modelProfileId","")),reasoningEffort=message.get("reasoningEffort", ""))
                    if not selected["modelProfileId"]:
                        choices=profiles()
                        selected["modelProfileId"]=next((p["id"] for p in choices if p.get("default")),choices[0]["id"] if choices else "")
                return dict(jobId=job.id,recordId=row.id,projectId=row.project_id,kind=kind,claim=job.claim,**selected)
        except Exception:
            # 没启动工作进程的领取失败也如实中断，不能保留一份永久 running 的幽灵工作。
            queue.publish(dict(op="finish",job={k:getattr(job,k) for k in ["id","kind","record_id","account_id","project_id","executor_id"]},claim=job.claim,status="interrupted"))
            raise


# 每次回传重新检查 claim、账号、项目和租约是否仍有效；拿到任务一次不等于永久有权写回。
# 功能：在事务内核验作业领取令牌及归属。
# 输入：db 是事务 Session，token 是 Job.claim 原文，不是模型 Key。
# 输出/影响：返回 (QueueJob, Record)；Redis 验证凭证与租约，MySQL 锁业务记录并核验归属和调度轮次，无效权限抛异常。
def authorized_claim(db,token):
    job=RedisJobs().for_claim(token)
    row=db.scalar(select(Record).where(Record.id==job.record_id).with_for_update())
    if job.account_id!=current_user() or job.project_id!=current_project() or not row or not owned(row) or row.project_id!=current_project() or row.payload.get("dispatchId")!=job.id:
        raise PermissionError("本地执行租约无效或任务不属于当前账号")
    return job,row


# 延长领取凭证的有效期并返回任务当前状态，让执行器能发现用户取消或中断。
# 功能：续约一个仍有效的执行作业。
# 输入：token 是执行器请求带来的领取凭证。
# 输出/影响：租约延长 45 秒并返回业务记录 status；不把 running 状态等同于测试通过。
def heartbeat(token):
    with session() as db:
        job,row=authorized_claim(db,token)
        RedisJobs().renew(job)
        return {"status":row.status}


# 过期租约标记为中断，不自动重跑：上一次操作可能已影响被测系统，只是没交回结果。
# 功能：把失去有效租约的执行器作业标记为中断。
# 输入：无参数；从 Redis 租约有序集合读取真正到期的 Job。
# 输出/影响：返回 None；关联记录有 executorId 时把过期 Job 标为 interrupted，仅将仍 running 的业务记录同步标为中断，不自动重跑。
def recover():
    # Redis 的有序集合只返回真正到期的租约，不再周期性查询 MySQL 的全部 running Job。
    expired=RedisJobs().expired()
    if not expired:return
    with session() as db:
        for job in expired:
            row=db.scalar(select(Record).where(Record.id==job.record_id).with_for_update())
            # 过期列表只是读取时的快照。心跳也先锁业务行，拿到锁后重新检查，避免把刚续租的工作中断。
            current=RedisJobs().get(job.id)
            if not current or current.claim!=job.claim or current.status!="running" or not current.lease or current.lease>now():continue
            # 已过期凭证无法续租；旧轮次的过期通知不能影响当前新的 dispatchId。
            if row and row.payload.get("dispatchId")==job.id and row.status in {"running","queued"}:
                row.status="interrupted";row.payload=dict(row.payload,status="interrupted",summary="执行器断开，请确认实际结果后继续")
            complete(db,job,"interrupted")


# 下发当前项目内允许该账号/任务读取的业务快照，私有模型配置与执行器记录不混入。
# 运行需要的 MCP 凭据由平台解密后下发给持有合法 claim 的执行器；模型 API Key 则由执行器自己的账号密钥存储提供，不在这份平台快照中。
# 功能：构造执行器可访问的项目数据副本。
# 输入：token 是当前作业的领取凭证；项目/账号由请求上下文限制。
# 输出/影响：返回 records/jobs 快照，过滤私有或其他身份数据，MCP 凭据按执行需求临时交付；不把平台数据库连接交给执行器。
def bundle(token):
    with session() as db:
        job,active=authorized_claim(db,token)
        rows=[]
        for r in db.scalars(select(Record)):
            if r.kind in PRIVATE_KINDS:continue
            if r.project_id and r.project_id!=current_project():continue
            if r.kind == "project" and r.id != current_project():continue
            if r.kind in {"task","run","mcp_probe", "local_action"} and r.payload.get("ownerId") and not owned(r):continue
            if r.kind in {"mobile_session","mobile-session"} and r.payload.get("accountId") and r.payload["accountId"]!=current_user():continue
            if r.kind=="network_session" and not network_owned(r,db):continue
            payload=dict(r.payload)
            if r.kind == "mcp_config":
                from .extensions import credentials
                payload["localCredentials"] = credentials(payload)
                payload.pop("credentials", None)
            rows.append(dict(id=r.id,kind=r.kind,status=r.status,projectId=r.project_id,payload=payload))
        identities={r["id"] for r in rows}
        # Job 只作为这次 HTTP 快照的 JSON 信息交付，SQLite 不再创建 jobs 表。
        jobs=[]
        for j in RedisJobs().for_records(r["id"] for r in rows if r["kind"] in {"task","run","mcp_probe","local_action"}):
            jobs.append(dict(id=j.id,kind=j.kind,record_id=j.record_id,account_id=j.account_id,project_id=j.project_id,executor_id=j.executor_id,status=j.status,claim=j.claim if j.id==job.id else "",lease=j.lease.isoformat() if j.lease else None))
        return dict(records=rows,jobs=jobs)


# 三方合并：base 是执行器读到的旧值，changed 是它提交的新值，current 是平台现值。
# 带 id 的列表按身份合并，尽量保留执行期间新来的用户消息；无法判断的冲突直接报错。
# 这里是应用层 JSON 合并，不是 Git，也不是数据库自带复制。
# 功能：对执行器旧值、修改值和平台当前值做三方合并。
# 输入：base 是领取/同步时的基线，changed 是执行器修改后值，current 是平台当前值。
# 输出/影响：返回合并后的 JSON 值；递归处理字典及带 ID 的列表，冲突抛异常，不直接写数据库。普通列表追加分支可能复用嵌套对象。
def merge(base,changed,current):
    """Three-way merge preserves new user messages and approvals during execution."""
    if changed==current:return copy.deepcopy(current)
    if changed==base:return copy.deepcopy(current)
    if current==base:return copy.deepcopy(changed)
    if isinstance(base,dict) and isinstance(changed,dict) and isinstance(current,dict):
        out=copy.deepcopy(current)
        for key,value in changed.items():
            if key not in base:
                if key in current and current[key]!=value:raise ValueError("并发写入冲突，请重新读取")
                out[key]=copy.deepcopy(value)
            else:out[key]=merge(base[key],value,current.get(key))
        for key in base.keys()-changed.keys():
            if current.get(key)!=base[key]:raise ValueError("并发删除冲突")
            out.pop(key,None)
        return out
    if isinstance(base,list) and isinstance(changed,list) and isinstance(current,list):
        if all(isinstance(v,dict) and "id" in v for group in [base,changed,current] for v in group):
            original={v["id"]:v for v in base};out=copy.deepcopy(current);positions={v["id"]:i for i,v in enumerate(out)}
            for v in changed:
                identity=v["id"]
                if identity in positions:out[positions[identity]]=merge(original.get(identity,{}),v,out[positions[identity]])
                elif identity not in original:out.append(copy.deepcopy(v))
            removed=set(original)-{v["id"] for v in changed}
            return [v for v in out if v["id"] not in removed]
        if changed[:len(base)]==base:return list(dict.fromkeys([*current,*changed[len(base):]])) if all(isinstance(v,str) for v in current+changed) else current+changed[len(base):]
    raise ValueError("并发写入冲突，请重新读取")


# 先校验可写记录类型、项目、账号及不可变字段，再把执行器提交的差异合并回平台。
# 尤其保护用户消息、权限和取消状态，避免子进程拿旧快照把用户的新决定覆盖掉。
# 执行端 Agent 在业务 SQLite 副本中新建 Run 后，平台接收该差异时补建 kind=test 的 Job，再由同一执行器的独立测试槽位领取。
# Test: Step16 [开始] 平台检查 claim、账号、项目和可写字段，合并差异后保存到 MySQL。
# 职责：平台接收执行器差异，校验领取权/账号/项目/可写字段，并与平台现值合并。
# 输入：token 是请求头 X-TestPilot-Claim 中的领取凭据；changes 是 replica 传来的变更列表，不是 Git diff。
# 输入：每项 base 是执行器旧快照，payload 是新值；平台现值从数据库重新读，不相信客户端自报归属。
# 返回：{"status":"ok"} 只说明同步请求被接收；它不是测试通过状态，也不是 Git 提交结果。
# 存储：MySQL 更新业务记录；Agent 新建 Run 时同事务登记 Outbox，再投递 Redis test Job，供独立测试槽领取。
# 边界：文件通过另一上传接口保存；三方合并尽量保留新用户消息和停止状态，无法安全合并时明确拒绝。
# 平台接收执行器差异的入口；token 是作业领取凭据，不是模型 API Key。
# 概念：这里 commit 是执行器结果同步接口，不是 Git 提交；token 是请求头传来的 Job claim，不是模型 Key、登录 Token 或 ContextVar 恢复句柄。
# authorized_claim 用 Redis 核验领取凭证和租约，用 MySQL 记录核验账号、项目与 dispatchId；只知道记录 ID 不能写回。
# with_for_update 在 MySQL 中锁住业务记录以保护并发更新；Job 领取由 Redis Lua 检查，不再使用 MySQL Job 行锁。
# 这里的三方合并比较 base=执行器旧快照、payload=执行器新值、row.payload=平台现值；尽量保留期间收到的新消息/取消/权限修改，无法安全合并则报错。
# MySQL records 保存业务状态，Redis 保存调度状态；返回 ok 仅说明同步被接收，测试结果仍看 Run 的真实报告。
def commit(token,changes):
    # 阶段一：限制批次大小，在平台事务内重新检查 claim、账号、项目和租约。
    # 执行器提交的 kind、ID、base、payload 都还要逐条校验；知道某个 ID 或持有旧快照不代表可以任意写回。
    if len(changes)>100:raise ValueError("同步记录过多")
    with session() as db:
        job,active=authorized_claim(db,token)
        for change in changes:
            identity,kind=change["id"],change["kind"]
            if kind not in EXECUTOR_WRITABLE:raise PermissionError("执行器不能修改该类平台记录")
            row=db.scalar(select(Record).where(Record.id==identity).with_for_update())
            base,payload=copy.deepcopy(change.get("base")),copy.deepcopy(change["payload"])
            # MCP 配置只允许回报连接检查结果。先移除临时下发的解密凭据，再限制可改字段。
            # 用户已修改配置版本时，旧连接的检查结果直接丢弃，避免它覆盖新配置的状态。
            if kind=="mcp_config":
                for value in [base,payload]:
                    if value:value.pop("localCredentials",None)
                if not row or not base:raise PermissionError("执行器不能创建 MCP 配置")
                altered={k for k in set(base)|set(payload) if base.get(k)!=payload.get(k)}
                if altered-{"status","tools","error","checkedAt"}:raise PermissionError("执行器只能更新 MCP 检查结果")
                if row.payload.get("revision")!=base.get("revision"):continue
            # 阶段二：已有记录先核对类型、项目、账号及当前作业的关联关系，再检查不可变归属字段。
            # 例如只能更新本次对话、检查和辅助动作；网络证据须属于当前执行，Run 也要满足下面的关联限制。
            if row:
                if kind in {"task","run","mcp_probe","local_action"} and not owned(row):raise PermissionError("记录不属于当前账号")
                if kind=="mcp_probe" and identity!=active.id:raise PermissionError("不能修改其他连接检查")
                if kind=="network_session" and row.payload.get("taskId")!=active.id and row.payload.get("ownerId")!=active.id:raise PermissionError("网络记录不属于当前执行")
                if kind=="network_session" and not network_owned(row,db):raise PermissionError("网络记录不属于当前账号")
                if kind=="local_action" and identity!=active.id:raise PermissionError("不能修改其他执行操作")
                if row.kind!=kind or (row.project_id and row.project_id!=current_project()):raise PermissionError("记录不属于当前项目")
                if kind=="task" and identity!=active.id:raise PermissionError("不能修改其他对话")
                if kind=="run" and row.payload.get("taskId") not in {active.id,"",None} and identity!=active.id:raise PermissionError("不能修改其他运行")
                for field in IMMUTABLE:
                    if field in row.payload and payload.get(field)!=base.get(field):raise PermissionError("不能修改记录归属")
                # Task 还要保护用户意图：执行器不能伪造用户消息、修改用户的模型选择或扩大 Agent 工具权限。
                # 权限/意图版本还原为基线后参与合并，平台的新设置会被保留；用户已停止时，迟到结果也不能把对话恢复为运行中。
                if kind=="task":
                    for message in payload.get("messages", []):
                        original=next((m for m in (base or {}).get("messages", []) if m["id"]==message["id"]), None)
                        if message.get("role")=="user" and (not original or any(message.get(f)!=original.get(f) for f in ["content","attachments","role","modelProfileId","reasoningEffort"])):
                            raise PermissionError("执行器不能伪造或修改用户消息")
                    for field in ["permissionMode","permissionVersion","intentVersion"]:
                        payload[field]=base.get(field,row.payload.get(field))
                    if row.status in {"cancelled","interrupted"}:
                        payload["status"]=row.status;payload["summary"]=row.payload.get("summary","")
                        base=dict(base,status=row.status,summary=row.payload.get("summary",""))
                # 阶段三：用旧基线、执行器新值和平台现值做三方合并，保留期间收到的新消息或设置。
                # 合并值写进整行 records.payload 的 JSON，status 同步到快捷查询列；冲突无法安全判断就报错，不静默覆盖。
                row.payload=merge(base or {},payload,row.payload)
                row.status=row.payload.get("status","")
            else:
                # 平台没有这个 ID 时，只允许当前执行产生的几类新记录，不能把失踪旧记录当新记录补回。
                # 项目、账号和执行器归属由平台填写；网络会话仍须验证与当前作业关联，执行器自填身份不能取代授权。
                if base is not None:raise ValueError("同步记录不存在")
                if kind not in {"case","run","network_session","mobile_session","mobile-session"}:raise PermissionError("不能创建该类记录")
                payload=dict(payload,projectId=current_project())
                if kind in {"case","run"}:payload["ownerId"]=current_user()
                if kind in {"mobile_session","mobile-session"}:payload.update(accountId=current_user(),executorId=active.payload.get("executorId",""))
                if kind=="network_session":
                    if payload.get("taskId")!=active.id and payload.get("ownerId")!=active.id:raise PermissionError("网络记录不属于当前执行")
                    payload["accountId"]=current_user()
                # Agent 新建 Run 时，平台还要登记独立 test Job，并绑定到这个 Task 使用的执行器。
                # Run 的 payload 保存本次输入和结果关联，Job 只登记待办；本事务提交后测试槽才能领取，API 在这里不运行 pytest。
                if kind=="run":
                    if payload.get("taskId")!=active.id or active.kind!="task":raise PermissionError("运行必须属于当前对话")
                    payload["executorId"]=active.payload["executorId"]
                row=Record(id=identity,kind=kind,project_id=current_project(),payload=payload,status=payload.get("status",""));db.add(row)
                if kind=="run":enqueue(db,row,"test")
    # 事务提交本批业务记录与待投递事件，随后投递 Redis；两种存储并不是一个普通数据库事务。
    # 返回 ok 只表示平台接收了这次同步；文件由另一接口落盘，测试是否通过仍看 Run 的实际报告。
    return {"status":"ok"}
    # Test: Step16 [结束] 测试路线到此完成：固定版本 → 真正执行 → 步骤与 pytest 证据 → 平台报告。
    # 如果从 Agent: Step10 的 runs_start 分支过来，现在回 tools.dispatch 的 runs_get 等待报告。


# 一次子进程结束后处理 Job：仍有待处理消息则重新排队，否则结束此次调度。
# Task 可以继续追问；每轮调度生成新的 Job ID，完成旧 Job 后再安排下一轮，不复用旧领取凭证。
# 功能：确认本份作业结束并决定是否安排下一轮。
# 输入：token 是当前有效 claim；从关联 Task 检查未交付消息或恢复意图。
# 输出/影响：登记旧 Job 的完成事件；有待处理消息时创建新一轮 Job。业务记录未回传终态时标记 error，返回 ok 不代表测试通过。
def finish(token):
    with session() as db:
        job,row=authorized_claim(db,token)
        pending=row.kind=="task" and row.status not in {"cancelled","waiting_approval"} and (row.payload.get("resumeRequested") or any(m["role"]=="user" and not m.get("delivered") and not m.get("suppressed") for m in row.payload.get("messages",[])))
        complete(db,job)
        if pending:
            row.status="queued";row.payload=dict(row.payload,status="queued",resumeRequested=False,summary="等待执行器继续")
            enqueue(db,row,"agent")
        elif row.status=="running":row.status="error";row.payload=dict(row.payload,status="error",summary="本地进程未回传终态，请恢复连接后继续")
    return {"status":"ok"}


# 功能：校验文件同步使用的简单相对路径。
# 输入：value 是相对平台数据目录的 POSIX 路径文本。
# 输出/影响：返回 PurePosixPath；拒绝绝对路径、反斜杠及空/点/上级目录段，不读取文件。
def valid_relative(value):
    path=PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or any(p in {"",".",".."} for p in value.split("/")):raise ValueError("文件路径不合法")
    return path


# 文件上传/下载既检查路径格式，也检查当前任务对该 Case/Run/资料的归属。
# 仅检查“路径在 data 目录里”还不够，否则可能读到另一个任务的文件。
# 功能：核对作业是否允许读写某个数据文件。
# 输入：token 是 claim，relative 是相对路径，writing 表示上传还是下载。
# 输出/影响：允许时返回 None，否则抛异常；按目录/关联任务/项目/账号检查，不执行实际文件传输。
def authorize_file(token,relative,writing=False):
    path=valid_relative(relative)
    with session() as db:
        job,active=authorized_claim(db,token)
        if writing:
            if len(path.parts)<3 or path.parts[0] not in {"cases","runs","inspections"}:raise PermissionError("不能上传该类文件")
            if path.parts[0]=="runs" and path.parts[1]!=active.id:
                row=db.get(Record,path.parts[1])
                if not row or row.payload.get("taskId")!=active.id:raise PermissionError("不能上传其他运行产物")
            if path.parts[0]=="inspections":
                associated=path.parts[1] in {i["id"] for i in active.payload.get("inspections",[])}
                associated=associated or any(r.payload.get("taskId")==active.id and r.payload.get("ownerId")==path.parts[1] for r in db.scalars(select(Record).where(Record.kind=="network_session")))
                if active.kind!="task" or not associated:raise PermissionError("页面产物不属于当前对话")
            if path.parts[0]=="cases":
                row=db.get(Record,path.parts[1])
                if row and row.project_id!=current_project():raise PermissionError("不能修改其他项目用例")
            return
        allowed=False
        for row in db.scalars(select(Record)):
            if row.project_id and row.project_id!=current_project():continue
            p=row.payload
            if row.kind=="case":
                allowed=any(relative==r["path"] or relative in {str(PurePosixPath(r["path"]).parent/n) for n in r.get("files",{})} for r in p["revisions"])
            elif row.kind in {"source","attachment","application"}:allowed=relative in {p.get("path"),p.get("textPath")}
            elif row.kind=="run" and (not p.get("ownerId") or owned(row)):allowed=relative.startswith("runs/"+row.id+"/")
            elif row.kind=="network_session":allowed=network_owned(row,db) and relative.startswith(p.get("path", "!invalid")+"/")
            elif row.kind=="task" and owned(row):allowed=any(relative.startswith("inspections/"+i["id"]+"/") for i in p.get("inspections",[]))
            if allowed:break
        if not allowed:raise PermissionError("文件不属于已授权任务或项目")


# 设备操作或浏览器检查等由 local_action 保存工具参数，再创建 kind=local 的 Job，等待账号绑定的执行器交回实际结果；服务端模式的执行器位于内网执行服务。
# 平台 API 只等待辅助作业结果；真正命令或设备调用发生在执行器的 local 进程槽位，这里的 local 是作业种类，并不限定服务必须部署在用户本机。
# 功能：将显式本地操作安排给执行服务并等待结果。
# 输入：name/arguments 是工具名和参数，environment_id 是可选目标环境。
# 输出/影响：创建 local_action/local Job，默认最多轮询 60 秒，打开 APP 为 120 秒；返回工具结果，失败抛异常，超时记录 cancelled，平台不直接操作设备。
def local_tool(name, arguments, environment_id=""):
    """Queue an explicit local action; platform never connects to devices or browsers."""
    import time
    from .storage import update_record
    payload=dict(id=uid("ACTION"), ownerId=current_user(), executorId=selected_executor(), status="queued", name=name, arguments=arguments, environmentId=environment_id, summary="等待本地执行器")
    if not payload["executorId"]: raise ValueError("请先连接本地执行器")
    with session() as db:
        row=Record(id=payload["id"],kind="local_action",project_id=current_project(),status="queued",payload=payload)
        db.add(row);enqueue(db,row,"local")
    # Appium 创建原生会话本身允许等待 70 秒，外层还包含排队及记录同步。
    wait_seconds=120 if name=="mobile_action" and arguments.get("action")=="open" else 60
    deadline=time.monotonic()+wait_seconds
    while time.monotonic()<deadline:
        row=read_record(payload["id"],"local_action")
        if row["status"]=="completed":return row["result"]
        if row["status"]=="error":raise ValueError(row.get("summary","本地操作失败"))
        time.sleep(.5)
    update_record(payload["id"],lambda p:p.update(status="cancelled",summary="本地操作超时"))
    raise ValueError("本地执行器未及时响应，请在环境检测页面检查连接")


# 设备租约仍由平台统一仲裁；不能只在各执行器 SQLite 中加锁，否则两台电脑会同时抢到设备。
# 功能：按作业身份管理设备独占锁。
# 输入：token 绑定运行作业，device_id 是设备，operation 为 acquire/renew/release，owner 是 RUN/MOBILE 会话 ID，session_id 可记录 Appium 会话。
# 输出/影响：写 device_leases 的 90 秒租约或删除锁，返回设备/旧会话或状态；只管理锁，不直接操作手机。
def device_lease(token,device_id,operation,owner,session_id=""):
    from .storage import DeviceLease
    with session() as db:
        job,active=authorized_claim(db,token)
        import hashlib
        key=hashlib.sha256((current_user()+active.payload.get("executorId","")).encode()).hexdigest()[:10]+":"+owner
        if owner.startswith("RUN-") and owner!=active.id:raise PermissionError("不能占用其他测试运行的设备")
        if owner.startswith("MOBILE-"):
            mobile=db.get(Record,owner)
            if mobile and mobile.payload.get("accountId")!=current_user():raise PermissionError("设备会话不属于当前账号")
        device=db.scalar(select(Record).where(Record.id==device_id,Record.kind=="device").with_for_update())
        if not device:raise ValueError("设备不存在")
        lease=db.get(DeviceLease,device_id)
        if operation=="acquire":
            if lease and lease.owner!=key and lease.expires_at>now():raise ValueError("设备已被其他本地任务占用")
            previous=dict(url=lease.appium_url,session=lease.session_id) if lease else {}
            if not lease:lease=DeviceLease(device_id=device_id,owner=key,expires_at=now());db.add(lease)
            lease.owner=key;lease.expires_at=now()+timedelta(seconds=90);lease.session_id="";lease.appium_url=device.payload["appiumUrl"]
            return dict(device=device.payload,previous=previous)
        if not lease or lease.owner!=key:raise PermissionError("设备租约不属于当前本地任务")
        if operation=="renew":
            lease.expires_at=now()+timedelta(seconds=90)
            if session_id:lease.session_id=session_id
            return {"status":"ok"}
        if operation=="release":
            previous=dict(url=lease.appium_url,session=lease.session_id);db.delete(lease);return previous
        raise ValueError("设备租约操作不合法")
