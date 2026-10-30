# 学习导航：Test: Step15 解释执行端结果怎样回平台；Agent 的资产/消息更新也复用该同步机制。
# 执行器的数据适配层：通过 HTTP 拉取平台业务记录快照，在本作业 SQLite 副本中调用业务函数，再通过 HTTP 提交记录差异；这里的客户端是执行服务，不是网页。
# 此模块使客户端复用 testing.py 等业务代码；不是 MySQL 主从复制，也不是与平台共享数据库文件。

"""Per-job SQLite cache synchronized over authenticated HTTP; no platform DB access."""
import copy
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import httpx
from .config import settings, require_https_url

_active_db = ContextVar("testpilot_replica_db", default=None)
_uploaded = {}


# 请求同时携带账号令牌、项目 ID 和本次 claim；三者分别证明是谁、操作哪个项目、领取了哪份工作。
# 执行器与平台通信的统一 HTTP 包装；method 是方法，path 是接口路径。
# 功能：携带作业身份向平台发同步请求。
# 输入：method/path 是 HTTP 方法与平台路径，kwargs 包含 JSON/文件内容等；token/project/claim 来自子进程 settings。
# 输出/影响：返回 HTTPX Response，HTTP 错误抛异常；这是跨进程 HTTP 通信，不传 Python Session 对象。
def request(method, path, **kwargs):
    # 领取凭证和平台令牌只能送到 HTTPS 控制面；不接受明文 HTTP 配置。
    platform_url=require_https_url(settings.platform_url,"平台同步地址")
    # Bearer 证明账号身份，Project 限定项目，Claim 证明当前作业领取权；后端都会核对。
    headers={"Authorization":"Bearer "+settings.platform_token,"X-TestPilot-Project":settings.executor_project,"X-TestPilot-Claim":settings.executor_claim}
    # 平台管理的配套执行器还声明当前代运行账号；不是所有调用者都能随意冒充账号。
    if settings.platform_managed:headers["X-TestPilot-Managed-Account"]=settings.executor_user
    # 允许文件上传补 Content-Type 等头，并移除 kwargs 中重复的 headers 参数。
    headers.update(kwargs.pop("headers",{}))
    # 设置超时且不自动跟随重定向，避免凭据随重定向流向其他地址。
    response=httpx.request(method,platform_url+path,headers=headers,timeout=30,follow_redirects=False,**kwargs)
    # 先检查 HTTP 错误，失败就中断同步，不继续当成有效快照处理。
    if response.status_code>=400:
        # 失败时不记录 HTTP 响应对象的完整内容，避免响应正文、账号令牌或作业 claim 随异常输出。
        # 只报告状态码，不把响应正文和授权头写进错误日志。
        raise RuntimeError(f"平台同步失败（HTTP {response.status_code}），任务已停止")
    # 返回 HTTPX Response，调用方再读取 JSON 或文件字节。
    return response


# 用平台的授权快照刷新本地缓存；它是可重建的工作副本，不是平台的第二个权威数据库。
# 功能：用授权平台快照刷新本份作业的 SQLite 业务缓存。
# 输入：db 是本地 Session，snapshot 含 records 与授权的 Job 事实；Job 只放 db.info 内存，不建 SQLite 队列表。
# 输出/影响：新增/更新授权行并删除快照中已不存在的旧缓存行，flush 后返回 None；不直接改平台 MySQL。
def apply_snapshot(db, snapshot):
    from .storage import Record
    identities={r["id"] for r in snapshot["records"]}
    from sqlalchemy import select
    for row in db.scalars(select(Record)):
        if row.id not in identities:db.delete(row)
    for item in snapshot["records"]:
        row=db.get(Record,item["id"])
        if not row:row=Record(id=item["id"],kind=item["kind"],payload={},project_id=item["projectId"]);db.add(row)
        row.payload=copy.deepcopy(item["payload"]);row.status=item["status"];row.project_id=item["projectId"]
    db.flush()
    # Job 的权威数据在 Redis；这里只缓存本次授权快照，不能把 SQLite 当成第二份调度队列。
    db.info["jobs"]=copy.deepcopy(snapshot.get("jobs",[]))


# 记录中的路径只是元信息，脚本/报告等文件还要单独传输。
# 用内容哈希跳过未变化文件，并拒绝链接与超大文件；用例文件先于引用它的记录提交。
# 文件块：changes 为待同步记录列表；cases_only 默认False，True表示第一阶段只传用例文件。
# 返回：正常None；实际输出是POST /api/executors/file的字节上传，平台文件系统据授权路径保存文件。
# 数据：内容hash缓存_uploaded只在本进程内避免重复上传，不是永久上传记录，也不保证跨存储原子提交。
# changes 是结构化记录变更；文件要另行上传，单改数据库路径不会让平台拥有文件。
# 功能：将业务变更关联的源码和证据文件上传平台。
# 输入：changes 是记录差异列表，cases_only 只上传用例版本文件。
# 输出/影响：按目录上传普通且不超过 100 MB 的文件，内存摘要缓存跳过相同内容；返回 None，文件同步和业务合并不是单个事务。
def sync_files(changes, cases_only=False):
    # 先从变更记录找出需要传输的目录：用例版本、Run 报告、Task 页面观察及网络采集。
    # cases_only=True 只传用例文件，让随后提交的平台版本记录有源码可读；其他证据在记录提交后再上传。
    for change in changes:
        payload=change["payload"];roots=[]
        if change["kind"]=="case":roots=[settings.data / "cases" / change["id"]]
        elif not cases_only:
            if change["kind"]=="run":roots=[settings.data/"runs"/change["id"]]
            elif change["kind"]=="task":roots=[settings.data/"inspections"/i["id"] for i in payload.get("inspections",[])]
            elif change["kind"]=="network_session":roots=[settings.data/payload["path"]]
        for root in roots:
            if not root.is_dir():continue
            # 每个目录只上传普通文件，跳过符号链接、超大文件及当前进程已传过的相同内容。
            # 文件字节与相对路径通过 HTTP 交给平台，平台继续检查授权后落盘；_uploaded 只是内存摘要缓存，不是永久上传记录。
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
# Test: Step15 [开始] storage.session 在执行器角色转到这里：拉快照，在 SQLite 改记录，再提交差异。
# 职责：让执行器复用业务代码而不直连平台 MySQL：拉授权快照→本地修改→比对差异→HTTP回传。
# 输入：transaction 无函数参数，由 storage.session 的 with 调用；身份/项目/claim 从 settings 读取。
# 交付：yield db 将本 Job 的 SQLAlchemy Session 交给业务函数，不是返回 JSON；嵌套调用复用同一会话。
# 输出：业务结束后提交 changes 列表，每项含 id/kind/base（修改前）/payload（修改后），并同步关联文件。
# 存储：SQLite 是业务工作副本；MySQL records 是业务主库，Redis 是 Job 调度主库，Agent checkpoint 另存。
# 过程：先上传用例文件，再 POST /api/executors/commit 提交记录，之后上传日志、截图等产物。
# 边界：同步用于执行中的多次读写，不只在最后调用；HTTP、两种数据库和文件并非一个分布式原子事务。
# 把带 yield 的函数变成 with 上下文，业务函数可复用同一种 session 写法。
@contextmanager
# 概念：replica 指执行器的业务工作副本适配层，不是数据库主从复制；它让 executor 通过 HTTP 拉平台记录，在自己的 SQLite 里复用业务代码，再提交差异。
# storage.session 在 role=executor 且持有 claim 时转到这里；平台 role 则直接管理平台数据库事务，所以两端业务都能沿用 with session() 写法。
# SQLAlchemy Session/事务与 Agent checkpoint 分开：事务负责一批业务数据库修改的提交/回滚，checkpoint 负责图从哪继续。
# @contextmanager 的 yield db 把当前数据库 Session 交给外层业务代码；外层执行完再回到本函数计算变化，嵌套读取复用该 Session，不重新覆盖未提交缓存。
def transaction():
    # 阶段一：判断是否已有外层业务事务。嵌套调用复用同一 Session，不重新拉快照覆盖正在修改的数据。
    # 最外层才从平台取这份作业授权可读的记录，刷新本地 SQLite；它是工作副本，不是共享主库。
    nested=_active_db.get()
    if nested is not None:
        yield nested
        return
    from .storage import Session, Record
    from sqlalchemy import select
    snapshot=request("GET","/api/executors/bundle").json()
    with Session.begin() as db:apply_snapshot(db,snapshot)
    with Session.begin() as db:
        db.info["jobs"]=copy.deepcopy(snapshot.get("jobs",[]))
        # 概念：deepcopy 创建独立的旧值副本；若只保留同一个 payload 对象，业务原地修改后旧值也变了，便无法比较差异。
        # 提交的每项变化包含 base（当时读到的旧值）与 payload（执行器改后的值）；平台可能已收到新用户消息，下一步用三方合并保留它们。
        # 文件和记录分通道：先上传用例文件，再提交引用它的记录，最后上传报告/截图。HTTP、文件及两端数据库并非一个原子事务，途中失败仍需如实处理。
        # 阶段二：把修改前的 payload 留作独立基线，再把本地 Session 交给 with 内的业务函数。
        # case_save、update_record 等在这段时间改 SQLite 中的记录；_active_db 只为嵌套调用保存当前会话，退出后恢复旧绑定。
        # 下面的 token 是 ContextVar.set 返回的“恢复旧值”句柄，供 finally 收尾；它不是登录 Token 或平台作业 claim。
        before={r.id:copy.deepcopy(r.payload) for r in db.scalars(select(Record))}
        token=_active_db.set(db)
        try:
            yield db
            db.flush()
            # 阶段三：业务函数完成后比较前后 payload，只提交实际变化的记录，每项带旧 base 和新 payload。
            # 先传用例文件，再把差异交给平台 commit 合并，最后传报告/截图；失败抛出，不把多种存储说成一个原子事务。
            changes=[dict(id=r.id,kind=r.kind,base=before.get(r.id),payload=copy.deepcopy(r.payload)) for r in db.scalars(select(Record)) if r.payload!=before.get(r.id)]
            if changes:
                sync_files(changes,True)
                request("POST","/api/executors/commit",json={"changes":changes})
                sync_files(changes)
        finally:_active_db.reset(token)
    # Test: Step15 [结束] 下一步：Test: Step16 → src/testpilot/executors.py 的 commit。
    # HTTP 把差异交给平台；报告文件走 sync_files，结构化记录走 commit，两者不是分布式原子事务。


# 按需下载已授权的文件到本地 data，设置大小限制；不是把整个平台工作目录挂给客户端。
# 功能：按作业授权下载一个缺失的文件到本地副本。
# 输入：relative 是相对 settings.data 的文件路径，需保持在该目录内。
# 输出/影响：HTTP 下载后写本地文件并缓存摘要，最多 100 MB；返回 None，不直接连接平台数据库。
def download(relative):
    path=(settings.data/relative).resolve()
    if not path.is_relative_to(settings.data.resolve()):raise ValueError("文件路径超出本地工作区")
    response=request("GET","/api/executors/file",params={"path":relative})
    if len(response.content)>100_000_000:raise ValueError("文件超过本地下载上限")
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(response.content)
    _uploaded[relative]=hashlib.sha256(response.content).hexdigest()
