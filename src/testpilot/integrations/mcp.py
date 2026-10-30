# TestPilot 对外提供的 MCP Server；与 mcp_client.py 接入别人的工具方向相反。
# 函数的英文 docstring 会作为外部工具说明，学习注释放在 # 中，避免无意改变模型看到的 schema/描述。

from testpilot.steps import Step
"""MCP v1 stdio / Streamable HTTP, with business operations behind the HTTP SDK."""
import asyncio
import json
import os
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from .sdk import Client

server = FastMCP("TestPilot", host="127.0.0.1", streamable_http_path="/", stateless_http=True, json_response=True)


# 内嵌平台 MCP 已有认证上下文时直接 dispatch；独立 stdio 进程则通过 HTTP SDK 访问平台。
# 两个入口最终复用同一业务实现，不应该复制一份用例保存/执行逻辑。
# 功能：把 MCP 工具操作转给统一业务分发：内嵌服务复用当前上下文，独立进程走平台 HTTP SDK。
# 输入：name/arguments 是 MCP 工具名及参数；key 是幂等标识；task_id 可关联对话；项目上下文来自认证中间件或 SDK 环境配置。
# 输出/影响：返回实际工具结果；内嵌调用 dispatch，独立调用 Client.tool 并 finally 关闭；实际副作用按工具定义发生，不复制业务逻辑。
def call(name, arguments, key="", task_id=""):
    from ..workspace import project_context_var
    if project_context_var.get():
        from ..tools import dispatch
        return dispatch(name,arguments,call_id=key,task_id=task_id)
    client = Client(os.environ.get("TESTPILOT_URL", "https://127.0.0.1:8020"))
    try:
        return client.tool(name, arguments, key=key,task_id=task_id)
    finally:
        client.close()


# 功能：对外列出可访问的项目身份，让外部 Agent 选择后通过请求头/环境变量切换项目。
# 输入：无参数；内嵌服务使用当前认证上下文，独立 stdio 服务使用 SDK 配置的账号与平台地址。
# 输出/影响：返回项目记录列表；直接读业务记录或在线程中 GET /api/state，不创建项目、不更改选中项目。
@server.tool()
async def projects_list() -> list:
    """List project identities. Select via X-TestPilot-Project (HTTP) or TESTPILOT_PROJECT_ID (stdio/CLI)."""
    from ..storage import list_records
    from ..workspace import project_context_var
    if project_context_var.get():return list_records("project")
    client=Client()
    try:return (await asyncio.to_thread(client.request,"GET","/api/state"))["projects"]
    finally:client.close()


# 功能：提供当前项目的需求概览、目标环境等信息，供外部 Agent 开始测试设计。
# 输入：无参数；项目选择来自 MCP HTTP 的 X-TestPilot-Project 或 stdio 的 TESTPILOT_PROJECT_ID。
# 输出/影响：返回上下文字典；asyncio.to_thread 将同步业务调用移到线程，仅读记录，不生成或执行用例。
@server.tool()
async def project_context() -> dict:
    """Read the current project's requirements and target environment."""
    return await asyncio.to_thread(call, "project_context", {})


# 功能：对外搜索项目资料名称和已解析正文，空查询用于分页列出可用资料。
# 输入：query 是搜索文本；offset/limit 是条目偏移和本页数量；项目范围由认证/选择上下文限制。
# 输出/影响：返回资料简介列表；只读取项目资料索引，不上传、修改文件或一次返回全部长正文。
@server.tool()
async def sources_search(query: str = "", offset: int = 0, limit: int = 12) -> list:
    """Search project material names and parsed text; empty query lists active sources. Page with offset/limit."""
    return await asyncio.to_thread(call, "sources_search", dict(query=query, offset=offset, limit=limit))


# 功能：分页读取当前项目上传资料的解析正文，供外部 Agent 据需求编写测试。
# 输入：id 是 source 记录 ID；offset/limit 按字符选择片段；记录必须属于当前项目。
# 输出/影响：返回正文、可读状态及 nextOffset 等字典；读取资料记录/解析文件，不执行文档指令或修改资料。
@server.tool()
async def sources_read(id: str, offset: int = 0, limit: int = 8000) -> dict:
    """Read a project's uploaded document or text material in bounded pages; unreadable files return a reason."""
    return await asyncio.to_thread(call, "sources_read", dict(id=id, offset=offset, limit=limit))


# 功能：分页读取上传附件内容，复用附件工具的归属与可读性检查。
# 输入：id 是附件 ID；offset/limit 按字符计；调用受当前项目及账号权限约束。
# 输出/影响：返回文本片段与读取信息字典；只读附件记录/文件，不把附件当程序运行，也不新增对话。
@server.tool()
async def attachments_read(id: str, offset: int = 0, limit: int = 8000) -> dict:
    """Read an uploaded document by ID within the selected project, in bounded pages."""
    return await asyncio.to_thread(call, "attachments_read", dict(id=id, offset=offset, limit=limit))


# 功能：让外部 Agent 查看当前项目已有可执行用例及其版本信息。
# 输入：无参数；当前项目通过 MCP 认证与选择上下文确定。
# 输出/影响：返回用例列表；仅查询业务记录，不编译新版本、不运行测试。
@server.tool()
async def cases_list() -> list:
    """List executable cases and their latest revisions."""
    return await asyncio.to_thread(call, "cases_list", {})


# 功能：读取一条用例及不可变版本内容，供外部 Agent 检查或基于旧版本修订。
# 输入：id 是当前项目 Case ID，而非 Run ID 或执行版本号。
# 输出/影响：返回用例详情字典；读取记录和对应源码资产，不修改历史版本或执行测试。
@server.tool()
async def cases_read(id: str) -> dict:
    """Read a case and all immutable source versions."""
    return await asyncio.to_thread(call, "cases_read", {"id": id})


# 功能：向外部 Agent 提供指定测试类型的可视化动作、字段与示例，避免编写不存在的步骤。
# 输入：kind 为 api/web/app/performance，表示要设计的测试类型。
# 输出/影响：返回能力目录字典；仅读程序内步骤 schema/说明，不启动对应引擎或写用例。
@server.tool()
async def steps_capabilities(kind: str) -> dict:
    """Discover visual actions, fields, examples and control blocks; use before authoring steps."""
    return await asyncio.to_thread(call,"steps_capabilities",dict(kind=kind))


# 外部 Agent 可保存 steps 或原生资产；内部 Agent 的可视化优先窄 schema 在 runtime 中单独注册。
# 功能：把外部 Agent 提交的可视化步骤或原生脚本保存为正式可执行版本。
# 输入：name/kind 为用例名称/类型；steps 与 code 选择内容，entry/files/config 为入口及辅助资产/运行配置；case_id 空新建，description 为说明，idempotency_key 防重。
# 输出/影响：返回用例及版本信息；steps 转普通字典并交统一保存入口，写 Case/Revision 记录和版本文件；不在保存时启动测试。
@server.tool()
async def cases_save_revision(name: str, kind: str, code: str = "", case_id: str = "", description: str = "", entry: str = "test_case.py", files: dict | None = None, config: dict | None = None, idempotency_key: str = "", steps: list[Step] | None = None) -> dict:
    """Save visual steps (see Step schema) or native pytest/JMX with versioned files/config. Supports JSON queries, advanced locators/waits, and a Python escape-hatch step. Existing case_id appends an immutable version."""
    return await asyncio.to_thread(call, "cases_save_revision", dict(name=name, kind=kind, code=code, case_id=case_id, description=description, entry=entry, files=files or {}, execution_config=config or {}, steps=[s.model_dump() for s in steps] if steps is not None else None), idempotency_key)


# 功能：为指定用例版本提交一次真实测试执行并立即返回排队记录。
# 输入：case_id/revision_number 定位不可变版本；environment_id 选目标环境；timeout 为秒；overrides 临时覆盖运行配置，network_capture 控制 Web 记录，idempotency_key 防重。
# 输出/影响：返回 Run 字典及 ID；统一入口写运行快照和 Job，执行器随后领取并启动容器；此函数不等待完整执行或声称通过。
@server.tool()
async def runs_start(case_id: str, revision_number: int, environment_id: str = "", timeout: int = 120, overrides: dict | None = None, idempotency_key: str = "", network_capture: str | None = None) -> dict:
    """Queue a real container execution and return its run ID."""
    return await asyncio.to_thread(call, "runs_start", dict(case_id=case_id, revision_number=revision_number, environment_id=environment_id, timeout=timeout, overrides=overrides or {},network_capture=network_capture), idempotency_key)


# 功能：查询一次真实运行结果，按需在有界时间内等待终态便于外部 Agent 连续分析。
# 输入：id 是 Run ID；wait_seconds 默认 0，仅查询当前状态，非零由工具校验且最多 90 秒。
# 输出/影响：返回状态、报告摘要与证据路径字典；只读或等待既有运行，不创建测试或改断言。
@server.tool()
async def runs_get(id: str, wait_seconds: int = 0) -> dict:
    """Read a report; optionally wait up to 90 seconds for a terminal result."""
    return await asyncio.to_thread(call, "runs_get", dict(id=id, wait_seconds=wait_seconds))


# 功能：取消已提交的执行，保留已经产生的日志、截图和报告证据。
# 输入：id 是需要停止的 Run ID，统一业务入口检查项目归属与可取消状态。
# 输出/影响：返回取消后的 Run 信息；修改执行状态并请求停止其作业/容器，已有证据仍由运行目录保存。
@server.tool()
async def runs_cancel(id: str) -> dict:
    """Cancel execution while preserving existing evidence."""
    return await asyncio.to_thread(call, "runs_cancel", {"id": id})


# 功能：读取测试证据并转换成 MCP 可发送的文本/图片内容块。
# 输入：path 是工具返回的证据相对路径；offset/limit 按字符分页读取文本，图片由证据读取器提供 base64。
# 输出/影响：返回 TextContent/ImageContent 列表；读取授权的数据目录文件，不在 MCP 响应中返回内部 Python 对象，也不改变报告。
@server.tool()
async def artifacts_read(path: str, offset: int = 0, limit: int = 16000) -> list:
    """Read evidence text with pagination; images include base64 content."""
    return evidence(await asyncio.to_thread(call, "artifacts_read", dict(path=path, offset=offset, limit=limit)))


# 功能：把测试 Skill 的说明或资源交给外部 Agent 阅读，由调用方自行决定后续工具和脚本操作。
# 输入：name 是技能名；resource 默认 SKILL.md，也可选该技能目录中的辅助资源。
# 输出/影响：返回技能文本及资源信息字典；只读内置/项目技能，不自动运行脚本或固定入口命令。
@server.tool()
async def skills_read(name: str, resource: str = "SKILL.md") -> dict:
    """Read a testing Skill; no command is executed automatically."""
    return await asyncio.to_thread(call, "skills_read", dict(name=name, resource=resource))


# 功能：根据 HTTP 请求规格生成原生 JMeter 测试计划，供需要原生路线的外部 Agent 使用。
# 输入：requests 是请求字典列表，含方法、路径和请求参数等规格，由统一工具参数及生成器校验。
# 输出/影响：返回计划内容字典；只生成内存 JMX，不保存 Case、启动 JMeter 或对目标施压。
@server.tool()
async def performance_plan(requests: list[dict]) -> dict:
    """Build native JMX from HTTP request specs; does not save or execute it."""
    return await asyncio.to_thread(call, "performance_plan", {"requests": requests})

# 功能：向外部 Agent 列出当前项目 Android 应用版本及 APK 标识。
# 输入：无参数；应用列表受当前项目选择与访问权限限制。
# 输出/影响：返回应用字典列表，包括版本/哈希等；只查询管理记录，不安装或卸载手机应用。
@server.tool()
async def applications_list() -> list:
    """List Android application versions and APK hashes."""
    return await asyncio.to_thread(call, "applications_list", {})

# 功能：列出当前项目可用 Android 测试设备、Appium 地址和占用状态。
# 输入：无参数；当前项目通过 MCP 上下文确定。
# 输出/影响：返回设备记录列表；只读取设备配置和占用信息，不创建 Appium 会话或操作手机。
@server.tool()
async def devices_list() -> list:
    """List Android devices, Appium endpoints and exclusive occupancy."""
    return await asyncio.to_thread(call, "devices_list", {})

# 功能：请求执行端检查指定 Android 设备和 Appium 服务是否可连接。
# 输入：id 是设备配置 Record ID，关联 Appium 地址与设备 udid。
# 输出/影响：返回实际连通性检查字典；可能网络访问执行服务/Appium 并查询设备，不执行完整回归用例或修改设备配置。
@server.tool()
async def devices_check(id: str) -> dict:
    """Check connectivity to Appium and the configured Android device."""
    return await asyncio.to_thread(call, "devices_check", {"id": id})

# 功能：供外部 Agent 在探索阶段打开、观察和操作 Android，并以截图/XML 等证据回传。
# 输入：action 为 open/observe/click/input/swipe/back/close；device_id/app_id 选设备和应用，session_id 续用会话；selector/by 定位，text/direction 为动作参数，reset 为启动策略。
# 输出/影响：返回 MCP 文本/图片块；执行端可创建 Appium 会话并改变手机界面、保存探索证据，close 释放设备；这不同于正式 Run 回归报告。
@server.tool()
async def mobile_action(action: str, device_id: str = "", app_id: str = "", session_id: str = "", selector: str = "", by: str = "accessibility id", text: str = "", direction: str = "up", reset: str = "reset") -> list:
    """Observe/control Android through open, observe, click, input, swipe, back, close. Close exploration before regression."""
    return evidence(await asyncio.to_thread(call, "mobile_action", dict(action=action,device_id=device_id,app_id=app_id,session_id=session_id,selector=selector,by=by,text=text,direction=direction,reset=reset)))


# 功能：列出当前项目采集会话，或分页搜索某次会话的浏览器请求摘要。
# 输入：session_id 空时列会话，非空指定采集；query/errors_only 为筛选条件，offset/limit 为条目分页。
# 输出/影响：返回网络列表字典；只读当前权限范围的捕获记录，不代理全机流量、不重放请求或自动读取全部正文。
@server.tool()
async def network_list(session_id: str = "", query: str = "", errors_only: bool = False, offset: int = 0, limit: int = 20) -> dict:
    """List browser capture sessions or paged request summaries in the selected project."""
    return await asyncio.to_thread(call, "network_list", dict(session_id=session_id, query=query, errors_only=errors_only, offset=offset, limit=limit))


# 功能：分页读取某次已捕获请求的元信息或请求/响应正文，供外部 Agent 分析接口。
# 输入：session_id/request_id 定位捕获及请求；section 为 metadata/request_body/response_body，offset/limit 为正文字符分页。
# 输出/影响：返回内容与下一页信息；读取会话隔离的已有记录，未保存正文不会凭空补全，也不发送该请求。
@server.tool()
async def network_read(session_id: str, request_id: str, section: str = "metadata", offset: int = 0, limit: int = 8000) -> dict:
    """Read request metadata or bounded request_body/response_body pages; no replay."""
    return await asyncio.to_thread(call, "network_read", dict(session_id=session_id, request_id=request_id, section=section, offset=offset, limit=limit))


# 功能：调整已有 Agent 对话当前轮及后续浏览器操作的网络记录级别。
# 输入：task_id 定位对话；level 是 off/api/detailed；reason 是调整理由，工具分发时将 task_id 用作调用关联。
# 输出/影响：返回采集策略字典；更新 Task 的网络策略记录，仅影响未来请求；用户显式关闭时业务校验拒绝擅自重新开启。
@server.tool()
async def network_capture_set(task_id: str, level: str, reason: str) -> dict:
    """Set off/api/detailed for an existing Agent task's current turn and future operations."""
    return await asyncio.to_thread(call, "network_capture_set", dict(level=level, reason=reason), task_id=task_id)


# 功能：把含可选截图的工具结果转换为标准 MCP 内容块，保留文字元信息与图片。
# 输入：result 是工具返回字典；可有 image（base64）及 mimeType，其余字段作为 JSON 文字输出。
# 输出/影响：返回 TextContent 列表，存在 image 时追加 ImageContent；只转换内存结果，不保存文件、调用模型或执行动作。
def evidence(result):
    metadata={k:v for k,v in result.items() if k!="image"}
    blocks=[TextContent(type="text",text=json.dumps(metadata,ensure_ascii=False))]
    if "image" in result:blocks.append(ImageContent(type="image",data=result["image"],mimeType=result["mimeType"]))
    return blocks
