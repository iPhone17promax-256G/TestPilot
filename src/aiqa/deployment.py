"""部署边界：浏览器只访问 API，执行服务通过私有令牌按真实账号领取任务。"""
import secrets
from .config import settings


def managed_authorized(raw):
    return bool(settings.managed_executor and len(settings.managed_token) >= 32 and raw and
                secrets.compare_digest(raw, settings.managed_token))


def deployment_info():
    return dict(deploymentMode=settings.deployment_mode, loginRequired=settings.deployment_mode == "server",
                managedExecutor=settings.managed_executor,
                executionLocation="本机" if settings.deployment_mode == "local" else "服务器")


def require_admin(user):
    if not user or user.get("role") != "admin":
        raise PermissionError("只有管理员可以管理运行环境或账号")
