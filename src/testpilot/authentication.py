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
# 功能：把登录密码变成可校验的 PBKDF2 摘要。
# 输入：password 是待处理的密码；salt 是十六进制盐，省略时随机生成。
# 输出/影响：返回包含算法/轮数/盐/摘要的字符串；本函数不写库，也不能从摘要还原密码。
def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return f"pbkdf2_sha256$600000${salt}${digest}"

# 功能：核对明文密码与已保存摘要是否匹配。
# 输入：password 是用户提交的密码，encoded 是 users.password_hash 中的字符串。
# 输出/影响：返回布尔值；摘要格式不合法时返回 False，不建立登录会话。
def verify_password(password, encoded):
    try:
        method, rounds, salt, digest = encoded.split("$")
        calculated = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return method == "pbkdf2_sha256" and secrets.compare_digest(digest, calculated)
    except (ValueError, TypeError):
        return False

# 功能：判断是否已创建服务端模式账号。
# 输入：无参数；查询 users 表并排除本地固定 LOCAL 身份。
# 输出/影响：返回布尔值，不返回账号明细，也不创建用户。
def has_users():
    with session() as db:
        return db.scalar(select(User.id).where(User.id != "LOCAL").limit(1)) is not None

# 功能：从 ORM 用户对象挑出可以对外显示的字段。
# 输入：user 是 User 对象，不是用户名字符串。
# 输出/影响：返回 id/username/role 字典；不含密码摘要，不改原对象。
def public_user(user):
    return {"id": user.id, "username": user.username, "role": user.role}


# 功能：取得本地免登录模式使用的固定身份。
# 输入：无参数；查找 users 中 ID=LOCAL 的记录。
# 输出/影响：返回公开用户字典；不存在时创建禁用密码的本地管理员，随事务保存。
def local_identity():
    """本地免登录仍使用固定内部身份，让用例、模型和任务继续沿用账号归属检查。"""
    with session() as db:
        user = db.get(User, "LOCAL")
        if not user:
            user = User(id="LOCAL", username="local-workspace", password_hash="!disabled", role="admin")
            db.add(user)
        return public_user(user)


# 功能：列出执行服务需要绑定的账号身份。
# 输入：无参数；按 deployment_mode 判断本地或服务端模式。
# 输出/影响：返回公开用户列表；本地只返回固定身份并可触发其创建，服务端查询非 LOCAL 账号。
def managed_users():
    from .config import settings
    if settings.deployment_mode == "local": return [local_identity()]
    with session() as db:
        return [public_user(user) for user in db.scalars(select(User).where(User.id != "LOCAL"))]


# 功能：查找内部凭据所声明的账号。
# 输入：identity 是账号 ID，常来自内部请求的账号头。
# 输出/影响：存在时返回公开用户字典，否则 None；仅查询数据库。
def user_by_id(identity):
    with session() as db:
        user = db.get(User, identity)
        return public_user(user) if user else None

# 只允许在尚无账号时创建首个管理员；MySQL 锁覆盖“检查是否存在→创建”的竞争窗口。
# 功能：初始化服务端模式的首个管理员。
# 输入：username/password 由上层请求校验后传入。
# 输出/影响：返回公开用户信息；用 MySQL 命名锁保护初始化，保存密码摘要，已有账号时抛异常。
def setup_account(username, password):
    from sqlalchemy.orm import Session
    with engine.connect() as connection:
        if connection.scalar(text("SELECT GET_LOCK('testpilot_account_setup', 10)")) != 1:
            raise ValueError("账号初始化正在进行，请稍后重试")
        connection.commit()
        try:
            # 这里把 Session 的 expire_on_commit 设为 False，保留提交后 user 的公开属性用于组装登录响应；否则属性过期后可能需要重新查询，而 Session 已关闭时会报对象已脱离会话。
            with Session(bind=connection,expire_on_commit=False) as db, db.begin():
                if db.scalar(select(User.id).where(User.id != "LOCAL").limit(1)):
                    raise ValueError("管理员已创建，请登录")
                user = User(id=uid("USER"), username=username, password_hash=password_hash(password), role="admin")
                db.add(user)
            return public_user(user)
        finally:
            connection.execute(text("SELECT RELEASE_LOCK('testpilot_account_setup')"))

# 登录成功生成随机会话令牌，数据库保存其 SHA-256 摘要和到期时间。
# 真正的令牌交给浏览器作为 Cookie 使用；日志不应记录原始令牌。
# 功能：验证账号密码并建立七天登录会话。
# 输入：username/password 是登录表单输入；固定 LOCAL 账号不能通过此路径登录。
# 输出/影响：成功返回原始随机 token 与公开用户的二元组，失败 None；数据库只保存 token 摘要及到期时间。
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
# 功能：凭浏览器会话令牌或 API Token 查找账号。
# 输入：token 是调用方提交的原始令牌，空串表示未提供。
# 输出/影响：返回公开用户字典或 None；查询令牌摘要与用户，登录会话还要检查过期时间。
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

# 功能：撤销一条浏览器登录会话。
# 输入：token 是 Cookie 中的原始会话令牌。
# 输出/影响：返回 None；删除对应 login_sessions 行，API Token 不由本函数撤销。
def logout_account(token):
    with session() as db:
        row = db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest())
        if row:
            db.delete(row)


# 创建时只返回一次明文 API Token，方便用户交给 CLI/MCP；之后列表只展示元信息。
# 功能：创建供 CLI/MCP 等外部调用使用的账号 API Token。
# 输入：user_id 是归属账号，name 是用户为令牌填写的展示名称。
# 输出/影响：返回 id/name/token 字典，原始 token 只在创建结果给出；api_tokens 表保存摘要和归属。
def token_create(user_id,name):
    token=secrets.token_urlsafe(40)
    with session() as db:
        row=ApiToken(id=hashlib.sha256(token.encode()).hexdigest(),user_id=user_id,name=name)
        db.add(row)
    return {"id":row.id,"name":name,"token":token}


# 功能：查看当前账号的 API Token 元信息。
# 输入：user_id 是已认证账号 ID。
# 输出/影响：返回 ID/名称/创建时间列表，不返回原始 token，不改库。
def tokens_list(user_id):
    with session() as db:
        return [{"id":r.id,"name":r.name,"createdAt":r.created_at.isoformat()} for r in db.scalars(select(ApiToken).where(ApiToken.user_id==user_id))]


# 功能：撤销一条归属于指定账号的 API Token。
# 输入：user_id 是当前账号，identity 是令牌摘要记录 ID。
# 输出/影响：返回 None；归属核对通过后删除 api_tokens 行，找不到或越权抛异常。
def token_delete(user_id,identity):
    with session() as db:
        row=db.get(ApiToken,identity)
        if not row or row.user_id!=user_id:raise ValueError("找不到访问令牌")
        db.delete(row)
