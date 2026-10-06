# 平台任务代理：账号绑定的本地执行器通过 HTTP 领任务、续租、取数据、交结果。
# 它管理的是执行权和数据归属，既不调用模型，也不在平台启动测试容器。

"""Platform job broker. No model calls, browsers or execution occur here."""
import copy
import secrets
from datetime import timedelta
from pathlib import PurePosixPath
from sqlalchemy import select
from .storage import Record, Job, session, now, uid, read_record, list_records
from .workspace import current_project, current_user

PRIVATE_KINDS = {"executor", "model_profile"}
EXECUTOR_WRITABLE = {"mcp_config","case", "run", "task", "network_session", "mobile_session", "mobile-session", "device", "mcp_probe", "local_action"}
IMMUTABLE = {"accountId","ownerId", "executorId", "projectId", "modelProfileId", "environmentId", "createdAt", "id"}


def owned(row, owner=None):
    return row.payload.get("ownerId") == (owner or current_user())


def network_owned(row, db):
    payload=row.payload
    if payload.get("accountId") and payload["accountId"]!=current_user():return False
    parent=db.get(Record,payload.get("ownerId") if payload.get("kind")=="run" else payload.get("taskId"))
    return bool(parent and (not parent.payload.get("ownerId") or owned(parent)))


def profiles():
    with session() as db:
        return [r.payload for r in db.scalars(select(Record).where(Record.kind=="model_profile")) if owned(r)]


def profile(identity):
    with session() as db:
        r=db.get(Record,identity)
        if not r or r.kind!="model_profile" or not owned(r):raise ValueError("当前账号中找不到模型配置")
        return dict(r.payload)


def online(payload):
    from datetime import datetime
    try:return (now()-datetime.fromisoformat(payload["lastSeen"])).total_seconds()<25
    except (KeyError,ValueError):return False


def registry():
    with session() as db:
        return [dict(r.payload,online=online(r.payload)) for r in db.scalars(select(Record).where(Record.kind=="executor")) if owned(r)]


def register(identity,name,report):
    with session() as db:
        r=db.get(Record,identity) if identity else None
        if r and (r.kind!="executor" or not owned(r)):raise PermissionError("执行器归属不匹配")
        payload=dict(id=identity or uid("EXECUTOR"),ownerId=current_user(),name=name[:120],lastSeen=now().isoformat(),report=report)
        if r:r.payload=payload
        else:db.add(Record(id=payload["id"],kind="executor",payload=payload))
        return payload


def selected_executor(identity=""):
    rows=registry()
    if identity:
        item=next((r for r in rows if r["id"]==identity),None)
        if not item:raise ValueError("当前账号中找不到本地执行器")
        return item["id"]
    return next((r["id"] for r in rows if r["online"]),rows[0]["id"] if rows else "")


# 用 SELECT FOR UPDATE SKIP LOCKED 挑选待处理 Job：锁住本次选择，跳过别人已锁的行。
# 只分配归属当前账号、符合执行器绑定的工作；生成短期 claim 凭证，后续请求都要带它。
def claim(executor_id,kind):
    if kind not in {"agent","test","local"}:raise ValueError("任务类型不正确")
    if executor_id not in {r["id"] for r in registry()}:raise PermissionError("执行器未绑定当前账号")
    with session() as db:
        candidates=db.scalars(select(Job).where(Job.kind==kind,Job.status=="queued").order_by(Job.created_at).with_for_update(skip_locked=True)).all()
        for job in candidates:
            row=db.get(Record,job.record_id)
            if not row or not owned(row) or row.payload.get("executorId") not in {"",None,executor_id}:continue
            if row.status=="cancelled":job.status="cancelled";continue
            token=secrets.token_urlsafe(32)
            job.status,job.claim,job.lease="running",token,now()+timedelta(seconds=45)
            row.status="running"
            row.payload=dict(row.payload,status="running",executorId=executor_id,summary="本地执行器正在执行")
            selected={}
            if row.kind=="task":
                pending=[m for m in row.payload.get("messages",[]) if m.get("role")=="user" and not m.get("suppressed")]
                message=next((m for m in pending if not m.get("delivered")),pending[-1] if pending else {})
                selected=dict(modelProfileId=message.get("modelProfileId",row.payload.get("modelProfileId","")),reasoningEffort=message.get("reasoningEffort", ""))
                if not selected["modelProfileId"]:
                    choices=profiles()
                    selected["modelProfileId"]=next((p["id"] for p in choices if p.get("default")),choices[0]["id"] if choices else "")
            return dict(jobId=job.id,recordId=row.id,projectId=row.project_id,kind=kind,claim=token,**selected)
    return None


# 每次回传重新检查 claim、账号、项目和租约是否仍有效；拿到任务一次不等于永久有权写回。
def authorized_claim(db,token):
    job=db.scalar(select(Job).where(Job.claim==token,Job.status=="running").with_for_update())
    row=db.get(Record,job.record_id) if job else None
    if not row or not owned(row) or row.project_id!=current_project() or not job.lease or job.lease<now():
        raise PermissionError("本地执行租约无效或任务不属于当前账号")
    return job,row


# 延长领取凭证的有效期并返回任务当前状态，让执行器能发现用户取消或中断。
def heartbeat(token):
    with session() as db:
        job,row=authorized_claim(db,token)
        job.lease=now()+timedelta(seconds=45)
        return {"status":row.status}


# 过期租约标记为中断，不自动重跑：上一次操作可能已影响被测系统，只是没交回结果。
def recover():
    with session() as db:
        for job in db.scalars(select(Job).where(Job.status=="running",Job.lease<now()).with_for_update(skip_locked=True)):
            row=db.get(Record,job.record_id)
            if row and row.payload.get("executorId"):
                job.status="interrupted"
                if row.status=="running":
                    row.status="interrupted";row.payload=dict(row.payload,status="interrupted",summary="本地执行器断开，请恢复连接后继续")


# 下发当前项目内允许该账号/任务读取的业务快照，私有模型配置与执行器记录不混入。
# 任务需要使用的 MCP 凭据会解密后交给授权执行器；这不同于仅保存在本地的模型 Key。
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
        jobs=[dict(id=j.id,kind=j.kind,record_id=j.record_id,status=j.status,claim=j.claim if j.id==job.id else "",lease=j.lease.isoformat() if j.lease else None) for j in db.scalars(select(Job)) if j.record_id in identities]
        return dict(records=rows,jobs=jobs)


# 三方合并：base 是执行器读到的旧值，changed 是它提交的新值，current 是平台现值。
# 带 id 的列表按身份合并，尽量保留执行期间新来的用户消息；无法判断的冲突直接报错。
# 这里是应用层 JSON 合并，不是 Git，也不是数据库自带复制。
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
# 本地 Agent 新建 Run 后，平台在这里补建对应的 test Job，交给独立测试槽位领取。
def commit(token,changes):
    if len(changes)>100:raise ValueError("同步记录过多")
    with session() as db:
        job,active=authorized_claim(db,token)
        for change in changes:
            identity,kind=change["id"],change["kind"]
            if kind not in EXECUTOR_WRITABLE:raise PermissionError("执行器不能修改该类平台记录")
            row=db.scalar(select(Record).where(Record.id==identity).with_for_update())
            base,payload=copy.deepcopy(change.get("base")),copy.deepcopy(change["payload"])
            if kind=="mcp_config":
                for value in [base,payload]:
                    if value:value.pop("localCredentials",None)
                if not row or not base:raise PermissionError("执行器不能创建 MCP 配置")
                altered={k for k in set(base)|set(payload) if base.get(k)!=payload.get(k)}
                if altered-{"status","tools","error","checkedAt"}:raise PermissionError("执行器只能更新 MCP 检查结果")
                if row.payload.get("revision")!=base.get("revision"):continue
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
                row.payload=merge(base or {},payload,row.payload)
                row.status=row.payload.get("status","")
            else:
                if base is not None:raise ValueError("同步记录不存在")
                if kind not in {"case","run","network_session","mobile_session","mobile-session"}:raise PermissionError("不能创建该类记录")
                payload=dict(payload,projectId=current_project())
                if kind in {"case","run"}:payload["ownerId"]=current_user()
                if kind in {"mobile_session","mobile-session"}:payload.update(accountId=current_user(),executorId=active.payload.get("executorId",""))
                if kind=="network_session":
                    if payload.get("taskId")!=active.id and payload.get("ownerId")!=active.id:raise PermissionError("网络记录不属于当前执行")
                    payload["accountId"]=current_user()
                if kind=="run":
                    if payload.get("taskId")!=active.id or active.kind!="task":raise PermissionError("运行必须属于当前对话")
                    payload["executorId"]=active.payload["executorId"]
                    db.add(Job(id=uid("JOB"),kind="test",record_id=identity))
                row=Record(id=identity,kind=kind,project_id=current_project(),payload=payload,status=payload.get("status",""));db.add(row)
    return {"status":"ok"}


# 一次子进程结束后处理 Job：仍有待处理消息则重新排队，否则结束此次调度。
# Task 可继续追问，所以同一个 Task 的 Job 会经历多次领取，并非只执行一生一次。
def finish(token):
    with session() as db:
        job,row=authorized_claim(db,token)
        pending=row.kind=="task" and row.status not in {"cancelled","waiting_approval"} and (row.payload.get("resumeRequested") or any(m["role"]=="user" and not m.get("delivered") and not m.get("suppressed") for m in row.payload.get("messages",[])))
        job.status="queued" if pending else "finished";job.lease=None
        if pending:row.status="queued";row.payload=dict(row.payload,status="queued",summary="等待本地执行器继续")
        elif row.status=="running":row.status="error";row.payload=dict(row.payload,status="error",summary="本地进程未回传终态，请恢复连接后继续")
    return {"status":"ok"}


def valid_relative(value):
    path=PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or any(p in {"",".",".."} for p in value.split("/")):raise ValueError("文件路径不合法")
    return path


# 文件上传/下载既检查路径格式，也检查当前任务对该 Case/Run/资料的归属。
# 仅检查“路径在 data 目录里”还不够，否则可能读到另一个任务的文件。
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


# 需要本机环境的操作包装成 local_action + Job，再等待客户端交回结果。
# 平台等待结果不等于平台执行了设备命令；真正操作发生在本地 local 槽位。
def local_tool(name, arguments, environment_id=""):
    """Queue an explicit local action; platform never connects to devices or browsers."""
    import time
    from .storage import update_record
    payload=dict(id=uid("ACTION"), ownerId=current_user(), executorId=selected_executor(), status="queued", name=name, arguments=arguments, environmentId=environment_id, summary="等待本地执行器")
    if not payload["executorId"]: raise ValueError("请先连接本地执行器")
    with session() as db:
        db.add(Record(id=payload["id"],kind="local_action",project_id=current_project(),status="queued",payload=payload))
        db.add(Job(id=uid("JOB"),kind="local",record_id=payload["id"]))
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        row=read_record(payload["id"],"local_action")
        if row["status"]=="completed":return row["result"]
        if row["status"]=="error":raise ValueError(row.get("summary","本地操作失败"))
        time.sleep(.5)
    update_record(payload["id"],lambda p:p.update(status="cancelled",summary="本地操作超时"))
    raise ValueError("本地执行器未及时响应，请在环境检测页面检查连接")


# 设备租约仍由平台统一仲裁；不能只在各执行器 SQLite 中加锁，否则两台电脑会同时抢到设备。
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
