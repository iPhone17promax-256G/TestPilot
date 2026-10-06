# 客户端数据适配层：HTTP 拉取快照 → SQLite 执行业务函数 → HTTP 提交差异。
# 此模块使客户端复用 testing.py 等业务代码；不是 MySQL 主从复制，也不是与平台共享数据库文件。

"""Per-job SQLite cache synchronized over authenticated HTTP; no platform DB access."""
import copy
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import httpx
from .config import settings

_active_db = ContextVar("aiqa_replica_db", default=None)
_uploaded = {}


# 请求同时携带账号令牌、项目 ID 和本次 claim；三者分别证明是谁、操作哪个项目、领取了哪份工作。
def request(method, path, **kwargs):
    headers={"Authorization":"Bearer "+settings.platform_token,"X-AIQA-Project":settings.executor_project,"X-AIQA-Claim":settings.executor_claim}
    if settings.platform_managed:headers["X-AIQA-Managed-Account"]=settings.executor_user
    headers.update(kwargs.pop("headers",{}))
    response=httpx.request(method,settings.platform_url.rstrip("/")+path,headers=headers,timeout=30,follow_redirects=False,**kwargs)
    if response.status_code>=400:
        # Do not include response repr or credentials in error logs.
        raise RuntimeError(f"平台同步失败（HTTP {response.status_code}），任务已停止")
    return response


# 用平台的授权快照刷新本地缓存；它是可重建的工作副本，不是平台的第二个权威数据库。
def apply_snapshot(db, snapshot):
    from .storage import Record, Job
    identities={r["id"] for r in snapshot["records"]}
    from sqlalchemy import select
    for row in db.scalars(select(Record)):
        if row.id not in identities:db.delete(row)
    for item in snapshot["records"]:
        row=db.get(Record,item["id"])
        if not row:row=Record(id=item["id"],kind=item["kind"],payload={},project_id=item["projectId"]);db.add(row)
        row.payload=copy.deepcopy(item["payload"]);row.status=item["status"];row.project_id=item["projectId"]
    job_ids={j["id"] for j in snapshot["jobs"]}
    for row in db.scalars(select(Job)):
        if row.id not in job_ids:db.delete(row)
    db.flush()
    for item in snapshot["jobs"]:
        row=db.get(Job,item["id"])
        if not row:row=Job(id=item["id"],kind=item["kind"],record_id=item["record_id"]);db.add(row)
        row.status=item["status"];row.claim=item["claim"];row.lease=datetime.fromisoformat(item["lease"]) if item["lease"] else None
    db.flush()


# 记录中的路径只是元信息，脚本/报告等文件还要单独传输。
# 用内容哈希跳过未变化文件，并拒绝链接与超大文件；用例文件先于引用它的记录提交。
def sync_files(changes, cases_only=False):
    for change in changes:
        payload=change["payload"];roots=[]
        if change["kind"]=="case":roots=[settings.data / "cases" / change["id"]]
        elif not cases_only:
            if change["kind"]=="run":roots=[settings.data/"runs"/change["id"]]
            elif change["kind"]=="task":roots=[settings.data/"inspections"/i["id"] for i in payload.get("inspections",[])]
            elif change["kind"]=="network_session":roots=[settings.data/payload["path"]]
        for root in roots:
            if not root.is_dir():continue
            for path in root.rglob("*"):
                if not path.is_file() or path.is_symlink() or path.stat().st_size>100_000_000:continue
                relative=path.relative_to(settings.data).as_posix()
                data=path.read_bytes();digest=hashlib.sha256(data).hexdigest()
                if _uploaded.get(relative)==digest:continue
                request("POST","/api/executors/file",params={"path":relative},content=data,headers={"Content-Type":"application/octet-stream"})
                _uploaded[relative]=digest


# 最外层事务先拉快照，业务层在 SQLite 中修改，再对比前后 payload 提交变更。
# 嵌套 session 复用当前事务，避免内部函数调用再次覆盖外层正在修改的数据。
# HTTP、SQLite、平台 MySQL 和文件同步不构成一个分布式原子事务，失败需要如实中断/排查。
@contextmanager
def transaction():
    nested=_active_db.get()
    if nested is not None:
        yield nested
        return
    from .storage import Session, Record
    from sqlalchemy import select
    snapshot=request("GET","/api/executors/bundle").json()
    with Session.begin() as db:apply_snapshot(db,snapshot)
    with Session.begin() as db:
        before={r.id:copy.deepcopy(r.payload) for r in db.scalars(select(Record))}
        token=_active_db.set(db)
        try:
            yield db
            db.flush()
            changes=[dict(id=r.id,kind=r.kind,base=before.get(r.id),payload=copy.deepcopy(r.payload)) for r in db.scalars(select(Record)) if r.payload!=before.get(r.id)]
            if changes:
                sync_files(changes,True)
                request("POST","/api/executors/commit",json={"changes":changes})
                sync_files(changes)
        finally:_active_db.reset(token)


# 按需下载已授权的文件到本地 data，设置大小限制；不是把整个平台工作目录挂给客户端。
def download(relative):
    path=(settings.data/relative).resolve()
    if not path.is_relative_to(settings.data.resolve()):raise ValueError("文件路径超出本地工作区")
    response=request("GET","/api/executors/file",params={"path":relative})
    if len(response.content)>100_000_000:raise ValueError("文件超过本地下载上限")
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(response.content)
    _uploaded[relative]=hashlib.sha256(response.content).hexdigest()
