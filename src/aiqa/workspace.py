# 请求/任务的身份上下文：这里只携带当前项目和账号，不负责保存业务数据。
# ContextVar 是 Python 标准库能力；project_scope、user_scope 是本项目封装。

"""Project context shared by HTTP requests, Agent tools and workers."""
from contextvars import ContextVar
from contextlib import contextmanager

project_context_var = ContextVar("aiqa_project", default="")
DEFAULT_PROJECT = "PROJECT"
SCOPED_KINDS = {"case", "run", "task", "environment", "source", "application", "mobile-session", "mobile_session", "attachment", "skill", "mcp_config", "mcp_probe", "network_session", "local_action"}

def current_project():
    from .config import settings
    return project_context_var.get() or settings.executor_project or DEFAULT_PROJECT

# 进入 with 后，深层业务函数可通过 current_project() 获取同一个项目 ID。
# finally 恢复之前的值，避免本次操作的项目身份残留到后续操作中。
# 跨进程不会自动传递 ContextVar，所以执行器还要通过任务和环境变量设置身份。
@contextmanager
def project_scope(identity):
    token = project_context_var.set(identity or DEFAULT_PROJECT)
    try:
        yield
    finally:
        project_context_var.reset(token)

user_context_var = ContextVar("aiqa_user", default="")

def current_user():
    from .config import settings
    return user_context_var.get() or settings.executor_user or "service"

# 账号上下文与项目上下文分开：同一项目内，模型配置和执行任务仍需按账号检查归属。
@contextmanager
def user_scope(identity):
    token = user_context_var.set(identity)
    try:
        yield
    finally:
        user_context_var.reset(token)
