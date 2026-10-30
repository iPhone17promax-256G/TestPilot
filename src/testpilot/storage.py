# 标准部署的平台数据库是 MySQL；执行器 Job 子进程使用各自的 SQLite 业务副本并通过 HTTP 回传。开发配置也可使用 SQLite，平台数据库类型由 settings.database_url 决定。
# Record/Outbox、ORM、数据库 Session 和事务的入门说明见 src/testpilot/testing.py 的 Test: Step1/4；副本同步随 Test: Step15/16 就地讲解。

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint, create_engine, select, text, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .workspace import current_project, current_user, project_context_var, SCOPED_KINDS
from .config import settings


# 功能：生成数据库使用的 UTC 时间。
# 输入：无参数；不依赖用户所在时区。
# 输出/影响：返回不带 tzinfo 的 datetime，供数据库时间列与租约比较；不写库。
def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# 功能：生成带业务前缀的随机记录 ID。
# 输入：prefix 是 TC/RUN/TASK 等记录类别前缀。
# 输出/影响：返回“前缀-随机十六进制”字符串；生成 ID 不等于创建数据库记录。
def uid(prefix):
    return f"{prefix}-{uuid4().hex[:12].upper()}"


class Base(DeclarativeBase):
    pass


# 项目、用例、报告、对话等共用 records 表，通过 kind 区分记录类型。
# payload 是业务 JSON；project_id、status、时间戳单独成列，方便筛选和加锁。
# 这是当前项目的轻量存储设计，不是所有系统都应采用的通用数据库范式。
class Record(Base):
    """Typed aggregate payloads; indexed identity/status columns support job claiming."""
    __tablename__ = "records"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    project_id: Mapped[str] = mapped_column(String(48), default="", index=True)
    status: Mapped[str] = mapped_column(String(24), default="", index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


# 事务发件箱：和 Task/Run 的业务变更一同提交，表示还需要向 Redis 投递一个事件。
# record_id 指向业务记录；payload 保存要发送的事件，delivered_at 表示是否已送达。
# 它不负责领取或心跳：Job 的运行状态、claim 凭证和 lease 租约均由 Redis 调度层维护。
class DispatchEvent(Base):
    """Transactional outbox: a delivery receipt, never the live job/lease store."""
    __tablename__ = "dispatch_outbox"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(48), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# 幂等记录：同一个 scope/key 和相同参数重复提交时，复用此前结果。
# fingerprint 是参数摘要，防止调用者复用同一个 key 却偷偷改变操作内容。
class Mutation(Base):
    __tablename__ = "mutations"
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_mutation"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(160))
    fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)


# 登录密码保存不可逆哈希，仅需比较哈希；模型供应商 API Key 调用时必须能读出原值，另由执行器的 LocalVault/ServerVault 保存，不能用同样的不可逆哈希代替。
class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(16), default="member")


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(48), index=True)
    name: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(48), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


# 设备独占锁：一个 Android 设备同一时间交给一个执行主体，避免两套用例相互点击。
class DeviceLease(Base):
    __tablename__ = "device_leases"
    device_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    owner: Mapped[str] = mapped_column(String(48))
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    session_id: Mapped[str] = mapped_column(String(80), default="")
    appium_url: Mapped[str] = mapped_column(String(2000), default="")


engine = create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=300)
Session = sessionmaker(engine, expire_on_commit=False)


# 平台分支直接开启数据库事务：正常退出提交，异常退出回滚。
# executor 分支改走 replica.transaction：下载授权数据，在 SQLite 中处理，再通过 HTTP 提交差异。
# 业务函数虽然都写 with session()，role=executor 时实际进入 replica.transaction 操作 SQLite 副本；这里“客户端”是执行器进程，它不直接连接平台 MySQL。
# 功能：为业务代码提供事务或执行器副本事务。
# 输入：无显式参数；按当前进程 settings.role 和 executor_claim 选择数据库路径。
# 输出/影响：with 内得到 SQLAlchemy Session；正常退出提交，异常回滚；执行器分支由 replica 同步业务差异。
@contextmanager
def session():
    if settings.role == "executor" and settings.executor_claim:
        from .replica import transaction
        with transaction() as db:
            yield db
    else:
        with Session.begin() as db:
            yield db
            dispatch_ids = db.info.get("dispatch_ids", [])[:]
        # 先提交业务事务，才能投递 Redis。投递失败保留 outbox，由平台后台重试；不把已提交请求伪装成回滚。
        if dispatch_ids:
            from .job_queue import deliver_committed
            deliver_committed(dispatch_ids)


# 创建尚不存在的表，并处理少量历史字段兼容。
# MySQL GET_LOCK 防止多个进程同时初始化表结构；它不是每个业务请求都要拿的锁。
# 功能：创建缺失的表并兼容历史字段。
# 输入：无参数；使用本进程 engine 对应的数据库。
# 输出/影响：返回 None；SQLite 建表，MySQL 加初始化锁后建表/补列/回填项目归属。
def initialize():
    if engine.dialect.name == "sqlite":
        Base.metadata.create_all(engine)
        if settings.role in {"executor","gateway"} and "jobs" in __import__("sqlalchemy").inspect(engine).get_table_names():
            # 业务副本可以重建；旧缓存的 jobs 表不再参与调度。平台开发库由迁移器先备份再迁移。
            with engine.begin() as connection:connection.execute(text("DROP TABLE jobs"))
        return
    with engine.connect() as connection:
        acquired = connection.scalar(text("SELECT GET_LOCK('testpilot_schema', 60)"))
        if acquired != 1:
            raise RuntimeError("无法取得数据库初始化锁")
        try:
            Base.metadata.create_all(connection)
            if not connection.execute(text("SHOW COLUMNS FROM users LIKE 'role'")).all():
                connection.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(16) NOT NULL DEFAULT 'member'"))
                # 老版本只有初始账号；迁移时选一个现有账号管理安装，不能让所有账号都成为管理员。
                connection.execute(text("UPDATE users SET role='admin' WHERE id=(SELECT id FROM (SELECT id FROM users ORDER BY id LIMIT 1) AS initial_user)"))
            columns = connection.execute(text("SHOW COLUMNS FROM records LIKE 'project_id'")).all()
            if not columns:
                connection.execute(text("ALTER TABLE records ADD COLUMN project_id VARCHAR(48) NOT NULL DEFAULT ''"))
                connection.execute(text("CREATE INDEX ix_records_project_id ON records(project_id)"))
            connection.execute(text("UPDATE records SET project_id='PROJECT' WHERE kind IN ('case','run','task','environment','source','application','mobile-session','mobile_session') AND project_id=''"))
            connection.commit()
        finally:
            connection.execute(text("SELECT RELEASE_LOCK('testpilot_schema')"))


# SQLAlchemy 的 before_flush 回调，在新增记录写库前补上项目与部分记录的账号归属。
# 这是本项目注册的事件处理函数，不是数据库自动知道了当前登录人。
# 功能：在 ORM 写入新业务记录前补齐项目和部分记录的账号归属。
# 输入：db 是触发 before_flush 的数据库 Session；flush_context/instances 由 SQLAlchemy 传入，本函数不使用它们。
# 输出/影响：返回 None；修改 db.new 中 Record 的 project_id/payload，随原事务写库。
@event.listens_for(Session, "before_flush")
def assign_project(db, flush_context, instances):
    for row in db.new:
        if isinstance(row, Record) and row.kind in SCOPED_KINDS:
            row.project_id = row.project_id or current_project()
            row.payload = dict(row.payload, projectId=row.project_id)
            if row.kind in {"task", "run", "mcp_probe","local_action"}:
                row.payload.setdefault("ownerId", current_user())


# 读取后还要核验项目/账号，不能只相信调用者传来的 ID。
# 项目记录检查创建者/成员，部分私有记录还检查账号归属；这些检查不是按项目角色细分每个动作的完整 RBAC 权限体系。
# 功能：检查一条记录是否属于当前项目及可访问账号。
# 输入：row 是已查询的 Record；当前项目/账号来自 ContextVar。
# 输出/影响：允许时返回 None，不允许时抛异常；只校验内存中的记录，不修改数据库。
def assert_project(row):
    if row.kind == "project" and row.payload.get("ownerId") and current_user() not in {row.payload["ownerId"], *row.payload.get("members", [])}:
        raise PermissionError("你不是该项目的成员")
    if row.kind in {"model_profile", "executor"} and row.payload.get("ownerId") != current_user():
        raise PermissionError("记录不属于当前账号")
    if row.kind in {"task", "run", "mcp_probe","local_action"} and row.payload.get("ownerId") and row.payload["ownerId"] != current_user():
        raise PermissionError("记录不属于当前账号")
    if row.kind in {"mobile_session", "mobile-session", "network_session"} and row.payload.get("accountId") and row.payload["accountId"] != current_user():
        raise PermissionError("会话不属于当前账号")
    if project_context_var.get() and row.kind in SCOPED_KINDS and row.project_id != current_project():
        raise ValueError("当前项目中找不到该记录")


# 功能：列出当前身份能够访问的指定业务类型。
# 输入：kind 是 case/run/task 等记录类型；项目和账号由请求或任务上下文提供。
# 输出/影响：返回 payload 字典列表，按创建时间倒序；执行查询和归属检查，不改记录。
def list_records(kind):
    with session() as db:
        # 先只按窄字段排序，得到 ID 顺序。不要让 MySQL 把包含完整聊天、截图等
        # 内容的 JSON payload 一起送入 filesort：单条对话很大时，即使记录很少
        # 也可能报 1038（排序内存不足），导致整个 /api/state 无法加载。
        query = select(Record.id).where(Record.kind == kind)
        if kind in SCOPED_KINDS:
            query = query.where(Record.project_id == current_project())
        identities = list(db.scalars(query.order_by(Record.created_at.desc(), Record.id.desc())))
        # 再分批读取正文，不在 SQL 中重新排序大 JSON；两步复用当前事务，
        # 500 个 ID 一批也避免超过 SQLite 副本常见的 SQL 参数数量上限。
        records = {}
        for offset in range(0, len(identities), 500):
            batch = identities[offset:offset + 500]
            records.update((row.id, row) for row in db.scalars(select(Record).where(Record.id.in_(batch))))
        rows=[]
        for identity in identities:
            # READ COMMITTED 等隔离方式下，记录可能已被另一请求删除。
            row = records.get(identity)
            if row is None: continue
            try: assert_project(row)
            except PermissionError: continue
            rows.append(dict(row.payload, projectId=row.project_id) if kind in SCOPED_KINDS else dict(row.payload,id=row.id) if kind=="project" else row.payload)
        return rows


# 功能：根据记录 ID 读取并检查归属。
# 输入：identity 是业务记录 ID；kind 可选，用于要求记录必须属于该类型。
# 输出/影响：返回业务字典，部分类型补上 projectId/id；找不到或无权限抛异常，不改记录。
def read_record(identity, kind=None):
    with session() as db:
        row = db.get(Record, identity)
        if row is None or (kind and row.kind != kind):
            raise ValueError(f"找不到记录：{identity}")
        assert_project(row)
        return dict(row.payload, projectId=row.project_id) if row.kind in SCOPED_KINDS else dict(row.payload,id=row.id) if row.kind=="project" else row.payload


# 先锁定记录，再复制 JSON、交给 update 回调修改、重新赋值，最后由 session 提交。
# 重新赋值使 ORM 能识别 JSON 已变化；同步 status 列是为了后续队列和列表查询一致。
# 功能：在事务内锁定并更新一条业务 JSON。
# 输入：identity 是记录 ID；update 是接收可修改 payload 字典的回调。
# 输出/影响：返回更新后的 payload；数据库保存 JSON 与 status 列，回调返回值不作为本函数结果。
def update_record(identity, update):
    with session() as db:
        row = db.scalar(select(Record).where(Record.id == identity).with_for_update())
        if not row:
            raise ValueError(f"找不到记录：{identity}")
        assert_project(row)
        payload = dict(row.payload)
        update(payload)
        row.payload = payload
        row.status = payload.get("status", "")
        return payload
