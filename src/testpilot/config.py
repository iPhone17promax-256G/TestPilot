# 进程配置字段的定义入口。完整讲解已放在 Step 首次使用处：settings/role/deployment_mode 见 src/testpilot/testing.py 的 Agent: Step1。
# 只读测试路线时，settings/data/角色说明也在 src/testpilot/testing.py 的 Test: Step1；第一遍不必从这里补概念。

from pathlib import Path
from urllib.parse import urlsplit
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Pydantic Settings 将 TESTPILOT_ 前缀环境变量和 .env 转成带类型的属性。
# role 的实际分工和设置位置，随 Agent: Step1 → Step2/3 的启动链路讲解。
# data 是本进程看到的文件根目录；host_data 是 Docker 守护进程看到的宿主机路径，挂载时不能混淆。
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TESTPILOT_", env_file=".env", extra="ignore")
    role: str = "platform"
    # settings.deployment_mode 的 local/server 指整套平台的部署方式：local 使用受限回环访问免登录，server 启用账号登录；它由安装配置决定，与 Agent.permissionMode 的工具审批无关。
    deployment_mode: str = Field(default="server", pattern="^(local|server)$")
    managed_executor: bool = False
    managed_token: str = ""
    runtime_url: str = "https://executor:8021"
    # 平台与执行服务均使用原生 TLS；证书与私钥只读挂载，不将 CA 签名私钥交给服务。
    tls_cert_file: str = ""
    tls_key_file: str = ""
    platform_managed: bool = False
    max_worker_processes: int = Field(default=8, ge=3, le=48)
    max_agent_processes: int = Field(default=2, ge=1, le=16)
    release_image: str = "testpilot-runtime:0.2"
    appium_image: str = "testpilot-appium:0.1"
    appium_container: str = "testpilot-appium-1"
    platform_url: str = "https://127.0.0.1:8020"
    platform_token: str = ""
    executor_claim: str = ""
    executor_user: str = ""
    executor_project: str = ""
    executor_id: str = ""
    client_root: Path = Path(".client")
    database_url: str = "sqlite:///./.client/cache.sqlite"
    # 只有平台连接 Redis；执行器通过带账号身份的 HTTP 接口领取，子进程不获得队列密码。
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_prefix: str = "testpilot:jobs"
    # Qdrant 只向平台开放；向量 Key 保存在执行服务，不放在这些平台环境变量里。
    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str = ""
    job_lease_seconds: int = Field(default=45, ge=15, le=300)
    job_history_seconds: int = Field(default=2592000, ge=3600)
    data: Path = Path(".runtime")
    host_data: str = ""
    runner_image: str = "testpilot-runtime:0.1"
    performance_image: str = "testpilot-jmeter:0.1"
    target_network: str = "testpilot-targets"
    model: str = "ark-code-latest"
    model_url: str = ""
    model_key: str = ""
    max_steps: int = 24
    task_timeout: int = 900
    access_token: str = ""
    model_max_tokens: int = Field(default=12000, ge=1000, le=32000)
    model_extra_body: dict = Field(default_factory=dict)
    model_reasoning_effort: str = ""


settings = Settings()
settings.data.mkdir(parents=True, exist_ok=True)


def require_https_url(url: str, label: str = "平台地址") -> str:
    """校验控制面目标地址，先拒绝 HTTP，再组织带凭据的网络请求。

    只用于 TestPilot 平台和执行服务之间的通信；被测环境、模型供应商及
    用户接入的 MCP 地址有自己的协议规则，不受这个控制面校验器影响。
    错误信息不拼接原始 URL，避免误填在 URL 中的凭据进入日志。
    """
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and bool(parsed.hostname) and
                 parsed.username is None and parsed.password is None and
                 not parsed.query and not parsed.fragment)
        # 读取 port 也会拒绝非数字或超出范围的端口。
        parsed.port
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError(label + "必须使用有效的 HTTPS 地址，且不能包含凭据、查询参数或片段")
    return url.rstrip("/")


def tls_server_options() -> dict:
    """准备原生 HTTPS 启动参数；没有证书时失败，不回退到明文监听。

    TLS 在本进程终止，因此关闭 Uvicorn 对转发协议头的解释。真实协议由
    TLS 连接决定，调用方不能用 X-Forwarded-Proto 将 HTTP 冒充 HTTPS。
    HTTPX 的证书信任另由 SSL_CERT_FILE 指向的 CA 合集提供，保持验证开启。
    """
    cert = Path(settings.tls_cert_file).expanduser() if settings.tls_cert_file else None
    key = Path(settings.tls_key_file).expanduser() if settings.tls_key_file else None
    if cert is None or key is None or not cert.is_file() or not key.is_file():
        raise RuntimeError("缺少 HTTPS 证书或私钥，请先配置 TESTPILOT_TLS_CERT_FILE 和 TESTPILOT_TLS_KEY_FILE")
    return {"ssl_certfile": str(cert), "ssl_keyfile": str(key), "proxy_headers": False}


# 功能：取出模型请求要附带的供应商扩展参数。
# 输入：url 是调用方传入的供应商地址，当前实现未按它分支；扩展参数来自 settings.model_extra_body。
# 输出/影响：返回当前进程里的扩展参数字典，不发模型请求、不保存配置；此返回值不是测试用例 config。
def model_body(url):
    return settings.model_extra_body
