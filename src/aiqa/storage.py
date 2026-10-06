# 数据访问基础：平台使用 MySQL；任务子进程使用本地 SQLite 缓存配合 HTTP 回传。
# Record/Job 等是 SQLAlchemy ORM 映射，表示数据库中的表；session() 是项目封装的事务入口。

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint, create_engine, select, text, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .workspace import current_project, current_user, project_context_var, SCOPED_KINDS
from .config import settings


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


# 队列表：表示“有一件事待执行”，record_id 指向具体的 Task 或 Run 等记录。
# claim 是领取凭证；lease 是凭证到期时间，执行器必须持续心跳续期。
# Job 的完成表示本次调度结束，不等同于 Run 的测试断言通过。
class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    record_id: Mapped[str] = mapped_column(String(48), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    claim: Mapped[str] = mapped_column(String(48), default="")
    lease: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


# 登录密码保存不可逆哈希；这与本地模型 API Key 必须可读取以便调用供应商不同。
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
# 因此业务层虽沿用 with session()，客户端并没有直接连接平台 MySQL。
@contextmanager
def session():
    if settings.role == "executor" and settings.executor_claim:
        from .replica import transaction
        with transaction() as db:
            yield db
    else:
        with Session.begin() as db:
            yield db


# 创建尚不存在的表，并处理少量历史字段兼容。
# MySQL GET_LOCK 防止多个进程同时初始化表结构；它不是每个业务请求都要拿的锁。
def initialize():
    if engine.dialect.name == "sqlite":
        Base.metadata.create_all(engine)
        return
    with engine.connect() as connection:
        acquired = connection.scalar(text("SELECT GET_LOCK('aiqa_schema', 60)"))
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
            connection.execute(text("SELECT RELEASE_LOCK('aiqa_schema')"))


# SQLAlchemy 的 before_flush 回调，在新增记录写库前补上项目与部分记录的账号归属。
# 这是本项目注册的事件处理函数，不是数据库自动知道了当前登录人。
@event.listens_for(Session, "before_flush")
def assign_project(db, flush_context, instances):
    for row in db.new:
        if isinstance(row, Record) and row.kind in SCOPED_KINDS:
            row.project_id = row.project_id or current_project()
            row.payload = dict(row.payload, projectId=row.project_id)
            if row.kind in {"task", "run", "mcp_probe","local_action"}:
                row.payload.setdefault("ownerId", current_user())


# 读取后还要核验项目/账号，不能只相信调用者传来的 ID。
# 这里没有完整的项目成员和角色体系；项目隔离与部分记录的账号归属是两层不同检查。
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


def list_records(kind):
    with session() as db:
        query = select(Record).where(Record.kind == kind)
        if kind in SCOPED_KINDS:
            query = query.where(Record.project_id == current_project())
        rows=[]
        for row in db.scalars(query.order_by(Record.created_at.desc())):
            try: assert_project(row)
            except PermissionError: continue
            rows.append(dict(row.payload, projectId=row.project_id) if kind in SCOPED_KINDS else dict(row.payload,id=row.id) if kind=="project" else row.payload)
        return rows


def read_record(identity, kind=None):
    with session() as db:
        row = db.get(Record, identity)
        if row is None or (kind and row.kind != kind):
            raise ValueError(f"找不到记录：{identity}")
        assert_project(row)
        return dict(row.payload, projectId=row.project_id) if row.kind in SCOPED_KINDS else dict(row.payload,id=row.id) if row.kind=="project" else row.payload


# 先锁定记录，再复制 JSON、交给 update 回调修改、重新赋值，最后由 session 提交。
# 重新赋值使 ORM 能识别 JSON 已变化；同步 status 列是为了后续队列和列表查询一致。
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
