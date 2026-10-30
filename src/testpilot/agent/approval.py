"""TestPilot 权限规则与 LangChain 人工审批中间件的接入层。

权限规则决定某次调用允许、拒绝或需要人工确认；暂停、保存运行位置及
处理人工决定的流程交给 HumanInTheLoopMiddleware。这里不定义图节点或边。
"""
import json

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import ToolMessage

from ..storage import read_record
from ..testing import task_event
from . import permissions


class PermissionReviewMiddleware(HumanInTheLoopMiddleware):
    """为本轮内置和 MCP 工具接入账号、项目及对话的权限规则。"""

    @property
    def name(self):
        # 沿用旧中间件名字，使已经暂停在 permission_review.after_model 的
        # 对话仍能恢复。框架自己生成这个节点，业务不手动连接或调度它。
        return "permission_review"

    def __init__(self, task_id, reviewer, tools, scrub, save_answer):
        # 输入：task_id 是对话 ID；reviewer 是独立 AI 审批模型；tools 是
        # 本轮可调用工具；scrub 清理已知敏感值；save_answer 保存主模型正文。
        # 输出/数据：中间件留在内存；审批判断写 Task.approvals，运行恢复点
        # 由 create_agent 配置的 checkpointer 保存，两份数据用途不同。
        self.task_id = task_id
        self.reviewer = reviewer
        self.scrub = scrub
        self.save_answer = save_answer
        self.descriptions = {tool.name: tool.description for tool in tools}
        # 暂停之后某个 MCP 可能被停用，或下一轮连接失败。旧暂停点仍包含
        # 它的审批决定，恢复时必须消费原批次，不能按新工具列表缩短批次。
        # 这里只保留审批策略入口，不把已经不可用的工具重新注册给模型；
        # 真正执行前 record_tool 会阻止本轮工具目录中不存在的调用。
        waiting_tools = {
            entry["name"]
            for entry in read_record(task_id).get("approvals", [])
            if entry["decision"] == "ask"
            and entry["status"] in {"pending", "approved", "rejected", "stale", "denied"}
        }
        super().__init__(interrupt_on={
            name: {
                "allowed_decisions": ["approve", "reject"],
                "when": self.needs_approval,
                "description": self.describe_approval,
            }
            for name in self.descriptions.keys() | waiting_tools
        })

    def needs_approval(self, request):
        # 输入：LangChain 提供的 ToolCallRequest，包含模型原始 name/args/id。
        # permissions.review 按 Agent 的 auto/readonly/bypass 模式判断，并
        # 绑定参数、环境和用户要求版本；auto 必要时另发一次 AI 审批请求。
        # 返回 True 时，由官方中间件暂停整批调用；False 时仍要经过
        # record_tool 的实际执行前复查，不能将 False 理解为直接获准执行。
        call = request.tool_call
        entry = permissions.review(
            self.task_id, call, self.reviewer,
            self.descriptions.get(call["name"], "未知工具"), self.scrub,
        )
        # 恢复时批准/拒绝已改变 status，但原来的 ask 集合和次序必须保持，
        # 框架才能把每个人工决定配回原调用。旧决定通过 callId 复用，不重复
        # 请求审批模型；是否仍有执行权限由 record_tool 再次检查。
        return entry["decision"] == "ask"

    def describe_approval(self, call, state, runtime):
        # 官方暂停协议只包含工具名、参数与说明，不包含 TestPilot 审批 ID。
        # 用 description 的固定首行携带业务 ID，避免同名、同参数的多个
        # 调用被误配；暂停发布和恢复适配层会读取这个由本程序生成的 ID。
        entry = next(
            item for item in read_record(self.task_id).get("approvals", [])
            if item["callId"] == call["id"]
        )
        return f"TestPilot approval: {entry['id']}\n{entry['reason']}"

    def after_model(self, state, runtime):
        # 主模型说了什么先保存到 Task，让等待审批时也能看到操作说明。
        # 随后交给官方中间件完成整批暂停、消费人工决定和生成拒绝响应；
        # 这里无需调用 interrupt 或手动选择图的下一节点。
        self.save_answer(state)
        updates = super().after_model(state, runtime)
        if not updates:
            return updates
        # 人工拒绝会由框架直接生成 ToolMessage，不进入真正执行工具的
        # record_tool 包装器。补上 blocked 业务事件供页面展示，并保留原
        # tool_call_id；拒绝状态仍为 rejected/stale，不能记成执行失败。
        entries = {
            item["callId"]: item
            for item in read_record(self.task_id).get("approvals", [])
        }
        for message in updates.get("messages", []):
            if not isinstance(message, ToolMessage):
                continue
            entry = entries.get(message.tool_call_id)
            if entry is None:
                raise permissions.ApprovalConflict("拒绝结果缺少对应审批记录")
            reason = self.scrub(entry.get("resolutionReason") or entry["reason"])
            content = json.dumps({"notExecuted": True, "reason": reason}, ensure_ascii=False)
            task_event(self.task_id, entry["name"], entry["arguments"],
                       content, message.tool_call_id, "blocked")
            message.content = content
        return updates
