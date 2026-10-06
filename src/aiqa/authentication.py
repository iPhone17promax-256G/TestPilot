# 账号与登录凭据：密码用哈希验证，登录会话和 API Token 也只在平台保存摘要。
# 模型供应商的 API Key 另由 local_vault.py 管理，不能把这三种凭据混为一谈。

"""Local accounts and revocable sessions; passwords and tokens are never returned."""
import hashlib
import secrets
from datetime import timedelta
from sqlalchemy import select, text
from .storage import User, LoginSession, ApiToken, session, engine, now, uid

# 随机盐使相同密码产生不同哈希；PBKDF2 多轮计算增加离线猜密码的成本。
# 哈希不是加密，后端通过重新计算来校验密码，不需要把原密码解密出来。
def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return f"pbkdf2_sha256$600000${salt}${digest}"

def verify_password(password, encoded):
    try:
        method, rounds, salt, digest = encoded.split("$")
        calculated = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return method == "pbkdf2_sha256" and secrets.compare_digest(digest, calculated)
    except (ValueError, TypeError):
        return False

def has_users():
    with session() as db:
        return db.scalar(select(User.id).where(User.id != "LOCAL").limit(1)) is not None

def public_user(user):
    return {"id": user.id, "username": user.username, "role": user.role}


def local_identity():
    """本地免登录仍使用固定内部身份，让用例、模型和任务继续沿用账号归属检查。"""
    with session() as db:
        user = db.get(User, "LOCAL")
        if not user:
            user = User(id="LOCAL", username="local-workspace", password_hash="!disabled", role="admin")
            db.add(user)
        return public_user(user)


def managed_users():
    from .config import settings
    if settings.deployment_mode == "local": return [local_identity()]
    with session() as db:
        return [public_user(user) for user in db.scalars(select(User).where(User.id != "LOCAL"))]


def user_by_id(identity):
    with session() as db:
        user = db.get(User, identity)
        return public_user(user) if user else None

# 只允许在尚无账号时创建首个管理员；MySQL 锁覆盖“检查是否存在→创建”的竞争窗口。
def setup_account(username, password):
    from sqlalchemy.orm import Session
    with engine.connect() as connection:
        if connection.scalar(text("SELECT GET_LOCK('aiqa_account_setup', 10)")) != 1:
            raise ValueError("账号初始化正在进行，请稍后重试")
        connection.commit()
        try:
            # 提交后仍需读取公开身份生成登录响应；默认过期会在关闭 Session 后触发 DetachedInstanceError。
            with Session(bind=connection,expire_on_commit=False) as db, db.begin():
                if db.scalar(select(User.id).where(User.id != "LOCAL").limit(1)):
                    raise ValueError("管理员已创建，请登录")
                user = User(id=uid("USER"), username=username, password_hash=password_hash(password), role="admin")
                db.add(user)
            return public_user(user)
        finally:
            connection.execute(text("SELECT RELEASE_LOCK('aiqa_account_setup')"))

# 登录成功生成随机会话令牌，数据库保存其 SHA-256 摘要和到期时间。
# 真正的令牌交给浏览器作为 Cookie 使用；日志不应记录原始令牌。
def login_account(username, password):
    with session() as db:
        user = db.scalar(select(User).where(User.username == username))
        if not verify_password(password, user.password_hash if user and user.id != "LOCAL" else password_hash("dummy-password")):
            return None
        token = secrets.token_urlsafe(40)
        db.add(LoginSession(id=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id, expires_at=now()+timedelta(days=7)))
        return token, public_user(user)

# 浏览器 Cookie 和外部调用的 Bearer Token 都在这里定位账号。
# 先查未过期登录会话，再查 API Token；返回的是公开账号信息，不含密码哈希。
def session_user(token):
    if not token:
        return None
    with session() as db:
        record = db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest())
        if record and record.expires_at > now():user_id=record.user_id
        else:
            api_token=db.get(ApiToken,hashlib.sha256(token.encode()).hexdigest())
            if not api_token:return None
            user_id=api_token.user_id
        user = db.get(User, user_id)
        return public_user(user) if user else None

def logout_account(token):
    with session() as db:
        row = db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest())
        if row:
            db.delete(row)


# 创建时只返回一次明文 API Token，方便用户交给 CLI/MCP；之后列表只展示元信息。
def token_create(user_id,name):
    token=secrets.token_urlsafe(40)
    with session() as db:
        row=ApiToken(id=hashlib.sha256(token.encode()).hexdigest(),user_id=user_id,name=name)
        db.add(row)
    return {"id":row.id,"name":name,"token":token}


def tokens_list(user_id):
    with session() as db:
        return [{"id":r.id,"name":r.name,"createdAt":r.created_at.isoformat()} for r in db.scalars(select(ApiToken).where(ApiToken.user_id==user_id))]


def token_delete(user_id,identity):
    with session() as db:
        row=db.get(ApiToken,identity)
        if not row or row.user_id!=user_id:raise ValueError("找不到访问令牌")
        db.delete(row)
