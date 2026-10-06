# 配置入口：同一套 Python 包会以 platform、gateway、executor 等不同角色启动。
# 先看 AIQA_ROLE 再判断代码在哪里运行；共享 Settings 类不代表各进程共享内存或密钥。

from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Pydantic Settings 将 AIQA_ 前缀环境变量和 .env 转成带类型的属性。
# platform 是保存数据的 API；gateway 是本机常驻网关；executor 是领取任务后的子进程。
# data 是本进程看到的文件根目录；host_data 是 Docker 守护进程看到的宿主机路径，挂载时不能混淆。
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIQA_", env_file=".env", extra="ignore")
    role: str = "platform"
    # 部署模式只由安装配置决定，浏览器不能通过按钮关闭服务端认证。
    deployment_mode: str = Field(default="server", pattern="^(local|server)$")
    managed_executor: bool = False
    managed_token: str = ""
    runtime_url: str = "http://executor:8021"
    platform_managed: bool = False
    max_worker_processes: int = Field(default=8, ge=3, le=48)
    max_agent_processes: int = Field(default=2, ge=1, le=16)
    release_image: str = "aiqa-runtime:0.2"
    appium_image: str = "aiqa-appium:0.1"
    appium_container: str = "aiqa-appium-1"
    platform_url: str = "http://127.0.0.1:8020"
    platform_token: str = ""
    executor_claim: str = ""
    executor_user: str = ""
    executor_project: str = ""
    executor_id: str = ""
    client_root: Path = Path(".client")
    database_url: str = "sqlite:///./.client/cache.sqlite"
    data: Path = Path(".runtime")
    host_data: str = ""
    runner_image: str = "aiqa-runtime:0.1"
    performance_image: str = "aiqa-jmeter:0.1"
    target_network: str = "aiqa-targets"
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


def model_body(url):
    return settings.model_extra_body
