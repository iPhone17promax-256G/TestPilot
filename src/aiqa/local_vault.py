# 模型密钥的本机存储：优先系统凭据库，否则使用账号目录中的权限受限文件。
# 文件权限不是额外加密；当前容器/WSL 通常使用文件回退，不能声称 Key 已端到端加密保存。

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
# 密钥不会因此进入平台 MySQL；本机账户/文件权限仍是文件回退方式的信任边界。
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
    def __init__(self, account):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account): raise ValueError("账号 ID 不合法")
        self.root = settings.client_root / "users" / account
        self.path = self.root / "model-keys.json"
        self.service = "AIQA:" + str(self.root.resolve())
        self.keyring = None
        try:
            import keyring
            backend = keyring.get_keyring()
            if backend.priority > 0 and not any(x in type(backend).__module__ for x in ["fail", "plaintext", "chainer"]): self.keyring = keyring
        except Exception: pass

    @property
    def storage_mode(self): return "系统凭据库" if self.keyring else "本机权限受限文件（未额外加密）"

    def get(self, identity):
        if self.keyring:
            try: return self.keyring.get_password(self.service, identity) or ""
            except Exception: raise ValueError("系统凭据库不可用，请解锁后重试") from None
        return json.loads(self.path.read_text()).get(identity, "") if self.path.exists() else ""

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
    def for_profile(self, profile):
        bound_path=self.root/"model-endpoints.json"
        bound=json.loads(bound_path.read_text()) if bound_path.exists() else {}
        if bound.get(profile["id"])!=profile["baseUrl"].rstrip("/"):
            raise ValueError("模型地址发生变化，请在本地重新填写 API Key")
        return self.get(profile["id"])

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
def safe_error(error):
    # Provider error bodies and exception repr can include authorization headers.
    import httpx
    if isinstance(error, httpx.HTTPStatusError): return f"供应商返回 HTTP {error.response.status_code}，请检查密钥、模型权限和额度"
    if isinstance(error, httpx.RequestError): return "无法连接模型供应商，请检查地址和网络"
    return "模型调用失败，请检查配置；错误详情已隐藏以保护密钥"


class ServerVault(LocalVault):
    """服务端 Key 使用 Fernet 加密，主密钥放执行服务的私有卷，API/测试容器均不挂载。

    加密防止单独泄露凭据文件时直接拿到 Key；服务器管理员仍有能力读取运行中的密钥。
    主密钥必须与凭据文件一起做受控备份，丢失后无法恢复。
    """
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

    @property
    def storage_mode(self):return "服务器账号隔离文件（Fernet 加密；主密钥在私有执行卷）"

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

    def get(self, identity):
        data=json.loads(self.path.read_text()) if self.path.exists() else {}
        if identity not in data:return ""
        try:
            value=json.loads(self.cipher().decrypt(data[identity].encode()))
            if value["account"] != self.account or value["model"] != identity:raise ValueError()
            return value["key"]
        except Exception:raise ValueError("服务器模型凭据不可解密，请恢复主密钥备份或重新配置 API Key") from None

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


def model_vault(account):
    return ServerVault(account) if settings.deployment_mode == "server" and settings.managed_executor else LocalVault(account)
