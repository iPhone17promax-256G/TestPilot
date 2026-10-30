# Agent 会话的存档适配层：主流程使用 ConversationSession，不在 runtime 中拼装图快照或恢复指令。
# LangChain create_agent 仍使用 LangGraph；这里只集中处理账号存档、消息补齐和审批暂停恢复，不定义节点或边。
# Task.messages/events/approvals 是供页面查询的业务记录；checkpoint 另存模型消息与可恢复的执行进度，两者不能互相替代。

import time
from contextlib import ExitStack

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from ..attachments import message_content
from ..config import settings
from ..storage import read_record, update_record
from ..testing import take_user_messages, task_event
from . import permissions


# 业务异常：本轮 Agent 的决策/时间/图推进预算已经耗尽；runtime 只处理这项含义，不直接依赖 LangGraph 异常类型。
class AgentBudgetExceeded(RuntimeError):
    pass


# 功能：兼容不同版本的图快照，收集已经保存的暂停项，并按暂停 ID 去重。
# 输入：snapshot 来自本会话 Agent 的 get_state，暂停项可能放在顶层，也可能放在 tasks 中。
# 输出/影响：返回 interrupt 对象列表；只读取快照，不修改 Task、图状态或存档文件。
def checkpoint_interrupts(snapshot):
    found = {item.id: item for item in getattr(snapshot, "interrupts", ())}
    for task in snapshot.tasks:
        for item in task.interrupts:
            found[item.id] = item
    return list(found.values())


class ConversationSession:
    # 功能：为同一个 Task 准备会话存档的生命周期和本轮执行配置，尚不连接数据库或请求模型。
    # 输入：task_id 是长期对话 ID；maximum 是本轮账号模型配置的 maxSteps，限制主模型决策次数。
    # 输出/影响：构造会话对象；thread_id 继续使用 Task ID，递归预算保留原来的 maximum*3+10，数据此时只在内存。
    def __init__(self, task_id, maximum):
        self.task_id = task_id
        self._config = {
            "configurable": {"thread_id": task_id},
            "recursion_limit": maximum * 3 + 10,
        }
        self._stack = ExitStack()
        self._checkpointer = None
        self._initial = None
        self._prepared = False

    # 功能：进入 with 时打开当前账号的恢复点存储，交给后面的 LangChain Agent 使用。
    # 输入：无额外参数；执行角色、账号和路径来自当前工作子进程的 settings。
    # 输出/影响：返回本会话对象；executor 使用账号 checkpoints.sqlite，兼容分支使用 MySQL Saver，并初始化存档表。
    def __enter__(self):
        # 每份 Job 的业务 SQLite 已由 run_job 初始化；这里打开的是另外一份图存档。
        # 后续工作进程用同一账号目录和 Task ID，才能恢复此前模型消息、待续环节和审批暂停位置。
        try:
            if settings.role == "executor":
                from langgraph.checkpoint.sqlite import SqliteSaver

                path = settings.client_root / "users" / settings.executor_user / "checkpoints.sqlite"
                path.parent.mkdir(parents=True, exist_ok=True)
                self._checkpointer = self._stack.enter_context(SqliteSaver.from_conn_string(str(path)))
            else:
                uri = settings.database_url.replace("mysql+pymysql://", "mysql://").split("?")[0]
                self._checkpointer = self._stack.enter_context(PyMySQLSaver.from_conn_string(uri))
            self._checkpointer.setup()
        except BaseException:
            # with 的进入阶段报错也要关闭已经打开的连接，不留下半打开的存档会话。
            self._stack.close()
            self._checkpointer = None
            raise
        return self

    # 功能：退出 with 时关闭存档连接，保留已经写入的恢复点。
    # 输入：exc_type/exc_value/traceback 由 Python with 传入，描述会话体内是否发生异常。
    # 输出/影响：沿用底层上下文管理器的异常处理结果；不删除 checkpoint、Task 或报告文件。
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return self._stack.__exit__(exc_type, exc_value, traceback)
        finally:
            self._checkpointer = None
            self._prepared = False

    # 功能：向 create_agent 提供已经打开的存档读写器，主流程无需知道 SQLite/MySQL 的连接细节。
    # 输入：无额外参数；调用者应已经进入 ConversationSession 的 with 块。
    # 输出/影响：返回 saver 对象供框架保存/读取进度；尚未进入会话时抛 RuntimeError，本属性不自行请求模型。
    @property
    def checkpointer(self):
        if self._checkpointer is None:
            raise RuntimeError("请先打开 Agent 会话存档")
        return self._checkpointer

    # 功能：为本轮选择首次消息、续聊消息或审批恢复输入，隐藏 get_state 和 Command 等底层接口。
    # 输入：agent 是主流程用 create_agent 创建、已接入本会话 checkpointer 的 Agent。
    # 输出/影响：准备好返回 True；仍等用户审批则发布等待状态并返回 False；可能更新消息 delivered、迁移标记和旧授权。
    def prepare(self, agent):
        self._prepared = False
        previous = agent.get_state(self._config)
        task = read_record(self.task_id)
        suspended = checkpoint_interrupts(previous)
        incoming = self._incoming_messages(previous, task, suspended)

        # 有已保存的暂停点时，必须先处理它；不能因为又启动了工作进程就重新请求模型或从头跑工具。
        # permissions 的适配入口负责旧 approvalIds 与官方 HITL decisions 协议，避免会话层复制审批规则。
        if suspended:
            values = permissions.resume_value(task, suspended)
            if values is None:
                permissions.paused(self.task_id, suspended)
                return False
            initial = Command(resume=values)
            update_record(self.task_id, lambda p: p.update(resumeRequested=False))
        elif incoming:
            initial = self._message_input(previous, task, incoming)
        else:
            # 图还有待续环节时传 None 从原位置继续；已结束且没有新消息时才添加一条继续要求。
            initial = None if previous.next else {
                "messages": [
                    HumanMessage(content="继续上一轮未完成的工作", id="resume-" + str(time.time_ns()))
                ]
            }

        self._initial = self._migrate_history(previous, task, initial)
        self._prepared = True
        return True

    # 功能：领取本轮可消费的用户消息，并对照图历史补齐尚未进入存档的用户发言。
    # 输入：previous 是已有图快照；task 是当前 Task 字典；suspended 表示是否必须先恢复审批。
    # 输出/影响：返回消息字典列表；普通续聊会把符合本进程模型配置的连续消息标为 delivered，审批暂停时暂不领取。
    def _incoming_messages(self, previous, task, suspended):
        incoming = [] if suspended else take_user_messages(self.task_id)
        known = {getattr(message, "id", None) for message in previous.values.get("messages", [])}
        # 本轮模型已在 run_job 激活。属于另一模型/思考深度的未交付消息留给后续子进程，
        # 已交付但未进入图的用户消息则按 ID 补入，避免既漏掉要求又把同一句话重复交给模型。
        for message in read_record(self.task_id).get("messages", []):
            from ..models import active_selection

            if active_selection and not message.get("delivered"):
                continue
            if (
                message["role"] == "user"
                and not message.get("suppressed")
                and message["id"] not in known
                and message["id"] != self.task_id + "-goal"
                and not any(item["id"] == message["id"] for item in incoming)
            ):
                incoming.append(message)
        if not incoming and not previous.values.get("messages"):
            incoming = [dict(content=task["goal"], id=self.task_id + "-goal")]
        return incoming

    # 功能：把普通续聊转换成模型消息，并给旧的未完成工具请求补“未执行完”的结果说明。
    # 输入：previous 提供旧模型/工具消息；task 提供环境和事件；incoming 是本轮已选中的用户消息。
    # 输出/影响：返回 messages 增量字典；旧 running 事件改为中断错误，但不重新执行任何旧工具。
    def _message_input(self, previous, task, incoming):
        old = previous.values.get("messages", [])
        answered = {message.tool_call_id for message in old if isinstance(message, ToolMessage)}
        unfinished = [
            ToolMessage(
                content="用户已停止或调整上一轮操作，请依据最新消息决定后续行动",
                tool_call_id=call["id"],
            )
            for message in old
            for call in getattr(message, "tool_calls", [])
            if call["id"] not in answered
        ]
        # 这只是补完整“请求—结果”的消息关系，避免把中断历史交给供应商时缺少 tool_call 响应。
        # 外部操作究竟做没做完仍须核对；补一条消息不能据此把旧操作重新执行一遍。
        for incomplete in unfinished:
            existing = next(
                (event for event in task.get("events", []) if event.get("callId") == incomplete.tool_call_id),
                None,
            )
            if existing and existing.get("status") == "running":
                task_event(
                    self.task_id,
                    existing["name"],
                    existing.get("arguments", "{}"),
                    "此前执行中断，结果未完整回传；请核对已有记录，不能盲目重试",
                    incomplete.tool_call_id,
                    "error",
                )
        return {
            "messages": [
                *unfinished,
                *[
                    HumanMessage(
                        content=message_content(message) + "\n当前环境 ID：" + task["environmentId"],
                        id=message["id"],
                    )
                    for message in incoming
                ],
            ]
        }

    # 功能：执行端缺少原图存档时，从业务聊天记录补回部分文字背景，而不是假装完整恢复旧图。
    # 输入：previous 是本地快照，task 是业务 Task，initial 是此前准备的本轮输入。
    # 输出/影响：返回原输入或替换后的文字历史；读取最近 40 条记录，助手正文最多 12000 字，作废旧批准并写 checkpointMigrated。
    def _migrate_history(self, previous, task, initial):
        if (
            settings.role == "executor"
            and not previous.values.get("messages")
            and (
                task.get("approvals")
                or any(message.get("role") == "assistant" for message in task.get("messages", []))
            )
        ):
            # 这里只重建已交付用户消息与助手正文，不把历史 tool_calls 放回待执行队列，
            # 不恢复缺失的截图/工具完整结果，也不凭旧批准继续修改外部系统。
            history = []
            for message in read_record(self.task_id).get("messages", [])[-40:]:
                if (
                    message.get("role") == "user"
                    and message.get("delivered")
                    and not message.get("suppressed")
                ):
                    history.append(
                        HumanMessage(
                            content=message_content(message) + "\n当前环境 ID：" + task["environmentId"],
                            id=message["id"],
                        )
                    )
                elif message.get("role") == "assistant" and message.get("content"):
                    history.append(AIMessage(content=message["content"][:12000], id=message["id"]))
            if history:
                initial = {"messages": history}

            # 功能：给业务 Task 标记文字历史迁移，并使缺少原图时无法安全复用的旧授权失效。
            # 输入：payload 由 update_record 注入，是最新可修改的 Task.payload。
            # 输出/影响：返回 None；原地改 approvals/checkpointMigrated，再由 update_record 保存并同步平台。
            def migrated(payload):
                permissions.invalidate(payload, "已改用本地恢复点，请按最新要求重新判断，旧操作不重放")
                payload["checkpointMigrated"] = True

            update_record(self.task_id, migrated)
        return initial

    # 功能：用 prepare 选定的本轮输入驱动 LangChain Agent，并把消息/状态流交给主流程显示。
    # 输入：agent 是 prepare 使用的同一个 Agent；调用者必须先 prepare 且取得 True。
    # 输出/影响：逐项 yield 原始 (mode, event)；框架保存 checkpoint，图推进超限转 AgentBudgetExceeded，正文与结论仍由 runtime 保存。
    def stream(self, agent):
        if not self._prepared:
            raise RuntimeError("请先准备本轮 Agent 消息或审批恢复")
        try:
            yield from agent.stream(
                self._initial,
                config=self._config,
                stream_mode=["messages", "updates"],
            )
        except GraphRecursionError as error:
            # 只翻译框架的推进预算异常；工具错误、存档失败等其他异常仍按原类型交给上层处理。
            raise AgentBudgetExceeded("Agent 决策或执行预算耗尽") from error

    # 功能：本轮流结束后，区分已存档的审批暂停与可供收尾的对话结果。
    # 输入：agent 是刚由 stream 推进的 Agent；读取同一 Task ID 的最终存档快照。
    # 输出/影响：有暂停则发布审批等待状态并返回 None；否则返回 state values，runtime 据此保存结论，不在此判定 Run 是否通过。
    def result(self, agent):
        snapshot = agent.get_state(self._config)
        suspended = checkpoint_interrupts(snapshot)
        if suspended:
            permissions.paused(self.task_id, suspended)
            return None
        return snapshot.values
