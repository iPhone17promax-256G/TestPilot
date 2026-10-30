# 请求/任务的身份上下文：这里只携带当前项目和账号，不负责保存业务数据。
# ContextVar 与 project_scope/user_scope 的完整入门说明在 src/testpilot/local_executor.py 的 Test: Step6 / Agent: Step3，第一遍按路线读即可。

"""Project context shared by HTTP requests, Agent tools and workers."""
from contextvars import ContextVar
from contextlib import contextmanager

project_context_var = ContextVar("testpilot_project", default="")
DEFAULT_PROJECT = "PROJECT"
SCOPED_KINDS = {"case", "run", "task", "environment", "source", "application", "mobile-session", "mobile_session", "attachment", "skill", "mcp_config", "mcp_probe", "network_session", "local_action", "rag_config", "rag_index"}

# 功能：取得当前请求或作业的项目身份。
# 输入：无参数；优先读项目 ContextVar，其次执行器配置，最后默认 PROJECT。
# 输出/影响：返回项目 ID 字符串；只读进程上下文，不查询数据库。
def current_project():
    from .config import settings
    return project_context_var.get() or settings.executor_project or DEFAULT_PROJECT

# 进入 with 后，深层业务函数可通过 current_project() 获取同一个项目 ID。
# finally 恢复之前的值，避免本次操作的项目身份残留到后续操作中。
# 跨进程不会自动传递 ContextVar，所以执行器还要通过任务和环境变量设置身份。
# 功能：在 with 块内临时设置项目身份。
# 输入：identity 是项目 ID，空值使用默认 PROJECT。
# 输出/影响：yield 不提供数据；进入时设置 ContextVar，退出时恢复原值，不写库且不自动跨进程传播。
@contextmanager
def project_scope(identity):
    token = project_context_var.set(identity or DEFAULT_PROJECT)
    try:
        yield
    finally:
        project_context_var.reset(token)

user_context_var = ContextVar("testpilot_user", default="")

# 功能：取得当前请求或作业的账号身份。
# 输入：无参数；优先读账号 ContextVar，其次执行器配置，最后 service。
# 输出/影响：返回账号 ID 字符串；不等于重新验证登录凭据。
def current_user():
    from .config import settings
    return user_context_var.get() or settings.executor_user or "service"

# 账号上下文与项目上下文分开：同一项目内，模型配置和执行任务仍需按账号检查归属。
# 功能：在 with 块内临时设置账号身份。
# 输入：identity 是已由上层验证的账号 ID。
# 输出/影响：yield 不提供数据；设置并在 finally 恢复 ContextVar，不创建账号或登录会话。
@contextmanager
def user_scope(identity):
    token = user_context_var.set(identity)
    try:
        yield
    finally:
        user_context_var.reset(token)
