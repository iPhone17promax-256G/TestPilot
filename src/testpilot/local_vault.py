# LocalVault/ServerVault 的保存方式与调用时机见 src/testpilot/local_executor.py 的 Agent: Step3；本文件保留具体凭据存取实现。
# 服务端自动托管模式使用 ServerVault，先用 Fernet 加密 Key 再写私有执行卷；本地文件回退没有该加密，两种方式都不能声称服务器/本机管理员无法读取运行中的 Key。

"""Account-specific local credentials; never synchronized to the platform."""
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from .config import settings

_vault_lock = threading.RLock()


# 在目标目录生成私有临时文件，写完后原子替换，避免中断留下半份 JSON。
# private_write 只负责私有文件的原子写入，不会自行加密 data；ServerVault 调用它前已加密 Key，LocalVault 文件回退写入的是明文。这些文件不存入平台模型配置表。
# 功能：将私有配置写入临时文件后替换目标。
# 输入：path 是调用者指定的私有配置路径，data 是可序列化 JSON。
# 输出/影响：返回 None；创建目录、写临时文件、os.replace 替换并清理临时文件，不提供跨多个文件的原子事务。
def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


# 用账号 ID 分目录和凭据命名空间；不接受路径形式的账号 ID，避免跨目录读写。
class LocalVault:
    # 功能：建立按账号隔离的执行端密钥库对象。
    # 输入：account 是合法账号 ID；根目录来自 settings.client_root。
    # 输出/影响：设置实例目录/文件/系统凭据服务名，优先选择可用安全 keyring；构造对象不读取或返回模型 Key。
    def __init__(self, account):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account): raise ValueError("账号 ID 不合法")
        self.root = settings.client_root / "users" / account
        self.path = self.root / "model-keys.json"
        self.service = "TestPilot:" + str(self.root.resolve())
        self.keyring = None
        try:
            import keyring
            backend = keyring.get_keyring()
            if backend.priority > 0 and not any(x in type(backend).__module__ for x in ["fail", "plaintext", "chainer"]): self.keyring = keyring
        except Exception: pass

    # 功能：说明当前实例实际使用的密钥保存方式。
    # 输入：self 是已初始化 LocalVault。
    # 输出/影响：返回系统凭据库或权限受限但未额外加密的本机文件说明，不读取密钥原文。
    @property
    def storage_mode(self): return "系统凭据库" if self.keyring else "本机权限受限文件（未额外加密）"

    # 功能：按模型配置 ID 读取 Key。
    # 输入：identity 是模型配置 ID，账号目录由 self 决定。
    # 输出/影响：返回原始 Key 或空串；读取系统凭据库或本机 JSON，凭据库不可用时报错。
    def get(self, identity):
        if self.keyring:
            try: return self.keyring.get_password(self.service, identity) or ""
            except Exception: raise ValueError("系统凭据库不可用，请解锁后重试") from None
        return json.loads(self.path.read_text()).get(identity, "") if self.path.exists() else ""

    # 功能：保存 Key，并可绑定对应供应商地址。
    # 输入：identity 是配置 ID，key 是原始凭据，base_url 可选；账号由 self 隔离。
    # 输出/影响：校验长度/换行后加锁写 keyring 或私有 JSON；有地址时更新 model-endpoints.json，返回 None。
    def put(self, identity, key, base_url=""):
        if not key or len(key) > 2048 or any(c in key for c in "\r\n"): raise ValueError("API Key 格式不合法")
        with _vault_lock:
            if self.keyring: self.keyring.set_password(self.service, identity, key)
            else:
                data = json.loads(self.path.read_text()) if self.path.exists() else {}
                data[identity] = key
                private_write(self.path, data)
            if base_url:
                bound_path=self.root/"model-endpoints.json"
                bound=json.loads(bound_path.read_text()) if bound_path.exists() else {}
                bound[identity]=base_url.rstrip("/");private_write(bound_path,bound)

    # 密钥同时绑定模型配置 ID 和供应商地址，平台元信息改地址后不能直接复用旧 Key。
    # 功能：确认 Key 与当前配置地址绑定后再读取。
    # 输入：profile 是包含 id/baseUrl 等字段的不含密钥的模型配置字典。
    # 输出/影响：返回 Key 或空串；地址变化抛异常要求重填，避免把旧供应商 Key 发到新地址。
    def for_profile(self, profile):
        bound_path=self.root/"model-endpoints.json"
        bound=json.loads(bound_path.read_text()) if bound_path.exists() else {}
        if bound.get(profile["id"])!=profile["baseUrl"].rstrip("/"):
            raise ValueError("模型地址发生变化，请在本地重新填写 API Key")
        return self.get(profile["id"])

    # 功能：删除一项 Key 及其地址绑定。
    # 输入：identity 是当前账号的配置 ID。
    # 输出/影响：返回 None；加锁删除系统凭据或私有文件条目，并同步删 model-endpoints.json 条目。
    def delete(self, identity):
        with _vault_lock:
            if self.keyring:
                try: self.keyring.delete_password(self.service, identity)
                except Exception: pass
            elif self.path.exists():
                data = json.loads(self.path.read_text()); data.pop(identity, None); private_write(self.path, data)
            bound_path=self.root/"model-endpoints.json"
            if bound_path.exists():
                bound=json.loads(bound_path.read_text());bound.pop(identity,None);private_write(bound_path,bound)


# 供应商原始错误可能回显请求头，所以只公开 HTTP 状态或连接失败等有限信息。
# 功能：把模型供应商异常转成不含凭据详情的提示。
# 输入：error 是 HTTPX 状态/连接异常或其他调用异常。
# 输出/影响：返回通用字符串，只保留必要 HTTP 状态，不输出原始异常正文，也不写日志。
def safe_error(error):
    # 供应商错误正文或异常对象可能含 Authorization 等请求头；safe_error 不直接返回这些原始内容，只给用户有限的状态码/连接失败说明。
    import httpx
    if isinstance(error, httpx.HTTPStatusError): return f"供应商返回 HTTP {error.response.status_code}，请检查密钥、模型权限和额度"
    if isinstance(error, httpx.RequestError): return "无法连接模型供应商，请检查地址和网络"
    return "模型调用失败，请检查配置；错误详情已隐藏以保护密钥"


class ServerVault(LocalVault):
    """服务端 Key 使用 Fernet 加密，主密钥放执行服务的私有卷，API/测试容器均不挂载。

    加密防止单独泄露凭据文件时直接拿到 Key；服务器管理员仍有能力读取运行中的密钥。
    主密钥必须与凭据文件一起做受控备份，丢失后无法恢复。
    """
    # 功能：建立服务器执行卷中的账号加密密钥库。
    # 输入：account 是账号 ID，复用 LocalVault 的目录和地址绑定方式。
    # 输出/影响：设置加密文件路径；若存在旧明文文件，将其内容迁移为加密项后删除旧文件。
    def __init__(self, account):
        super().__init__(account)
        self.account = account
        self.keyring = None
        self.path = self.root / "server-model-keys.json"
        legacy=self.root/"model-keys.json"
        # 单机账号升级到服务端模式：先写完加密副本再移除旧明文文件，失败时保留原文件。
        if legacy.exists():
            with _vault_lock:
                if legacy.exists():
                    values=json.loads(legacy.read_text())
                    for identity,key in values.items():self.put(identity,key)
                    legacy.unlink()

    # 功能：说明服务器密钥库的加密与归属方式。
    # 输入：self 是 ServerVault 实例。
    # 输出/影响：返回 Fernet 与私有执行卷的说明字符串，无读写副作用。
    @property
    def storage_mode(self):return "服务器账号隔离文件（Fernet 加密；主密钥在私有执行卷）"

    # 功能：读取或创建执行卷中的 Fernet 主密钥。
    # 输入：self 提供客户端私有根目录；无额外参数，供 get/put 调用。
    # 输出/影响：返回 Fernet 对象；Linux 文件锁保护首次生成，主密钥保存在 .secrets/model-master.key，需保留才能解密已有项。
    def cipher(self):
        from cryptography.fernet import Fernet
        path=settings.client_root / ".secrets" / "model-master.key"
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        # 文件锁覆盖创建和读取，避免另一个进程读到刚创建但尚未写完的主密钥。
        import fcntl
        with open(path.parent / "master.lock", "a+b") as lock:
            os.chmod(lock.name,0o600)
            fcntl.flock(lock,fcntl.LOCK_EX)
            if not path.exists():
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,"wb") as handle:handle.write(Fernet.generate_key())
            return Fernet(path.read_bytes())

    # 功能：解密某账号某模型配置的 Key。
    # 输入：identity 是配置 ID，self.account 指明账号隔离范围。
    # 输出/影响：返回原始 Key 或空串；验证解密载荷中的 account/model，不合法时抛通用错误。读取密文时 cipher() 可能初始化主密钥目录/文件。
    def get(self, identity):
        data=json.loads(self.path.read_text()) if self.path.exists() else {}
        if identity not in data:return ""
        try:
            value=json.loads(self.cipher().decrypt(data[identity].encode()))
            if value["account"] != self.account or value["model"] != identity:raise ValueError()
            return value["key"]
        except Exception:raise ValueError("服务器模型凭据不可解密，请恢复主密钥备份或重新配置 API Key") from None

    # 功能：加密保存服务器模式的模型 Key。
    # 输入：identity 是配置 ID，key 是原文，base_url 可选绑定地址。
    # 输出/影响：加锁将 account/model/key 封装后 Fernet 加密，写服务器私有 JSON 和地址绑定；返回 None，不写平台数据库。
    def put(self, identity, key, base_url=""):
        if not key or len(key)>2048 or any(c in key for c in "\r\n"):raise ValueError("API Key 格式不合法")
        with _vault_lock:
            data=json.loads(self.path.read_text()) if self.path.exists() else {}
            data[identity]=self.cipher().encrypt(json.dumps(dict(account=self.account,model=identity,key=key)).encode()).decode()
            private_write(self.path,data)
            if base_url:
                path=self.root/"model-endpoints.json"
                bound=json.loads(path.read_text()) if path.exists() else {}
                bound[identity]=base_url.rstrip("/");private_write(path,bound)


# 功能：按部署方式选择执行端密钥库实现。
# 输入：account 是归属账号；deployment_mode/managed_executor 来自 settings。
# 输出/影响：受管服务端模式返回 ServerVault，其他返回 LocalVault；返回的是存储对象，不是 Key。
def model_vault(account):
    return ServerVault(account) if settings.deployment_mode == "server" and settings.managed_executor else LocalVault(account)
