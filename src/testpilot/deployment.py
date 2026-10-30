"""部署边界：浏览器只访问 API，执行服务通过私有令牌按真实账号领取任务。"""
import secrets
from .config import settings


# 功能：验证平台与受管执行服务之间的内部令牌。
# 输入：raw 是请求中的原始令牌；安装配置必须启用 managed_executor 且令牌至少 32 字符。
# 输出/影响：返回布尔值；只比较凭据，不校验项目成员，也不改数据库。
def managed_authorized(raw):
    return bool(settings.managed_executor and len(settings.managed_token) >= 32 and raw and
                secrets.compare_digest(raw, settings.managed_token))


# 功能：向页面提供部署方式和执行位置说明。
# 输入：无参数；读取当前 settings 中的部署字段。
# 输出/影响：返回 deploymentMode/loginRequired/managedExecutor/executionLocation 字典，无副作用。
def deployment_info():
    return dict(deploymentMode=settings.deployment_mode, loginRequired=settings.deployment_mode == "server",
                managedExecutor=settings.managed_executor,
                executionLocation="本机" if settings.deployment_mode == "local" else "服务器")


# 功能：检查账号是否具有安装管理权限。
# 输入：user 是已认证的公开账号字典，role 指账号角色而非进程职责。
# 输出/影响：管理员时返回 None，否则抛 PermissionError；不改变账号角色。
def require_admin(user):
    if not user or user.get("role") != "admin":
        raise PermissionError("只有管理员可以管理运行环境或账号")
