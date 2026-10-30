# Typer 命令行入口：testpilot 子命令将参数转换为 HTTP 请求，不在 CLI 里另写执行引擎。
# CLI 供外部 Agent 或用户调用平台能力；启动测试只提交 Run/Job，真正测试由绑定执行器领取，本地模式在本机，服务端模式在内网执行服务。

import json
from pathlib import Path
import typer
from .sdk import Client

app = typer.Typer(no_args_is_help=True)
cases = typer.Typer(no_args_is_help=True)
runs = typer.Typer(no_args_is_help=True)
agent = typer.Typer(no_args_is_help=True)
mobile = typer.Typer(no_args_is_help=True)
performance = typer.Typer(no_args_is_help=True)
network = typer.Typer(no_args_is_help=True)
sources = typer.Typer(no_args_is_help=True)
app.add_typer(sources, name="source")
app.add_typer(network, name="network")
app.add_typer(mobile, name="mobile")
app.add_typer(performance, name="performance")
mcp = typer.Typer(no_args_is_help=True)
app.add_typer(cases, name="case")
app.add_typer(runs, name="run")
app.add_typer(agent, name="agent")
app.add_typer(mcp, name="mcp")


# 统一创建/关闭 SDK 客户端，输出 JSON；错误用非零退出码，便于脚本判断失败。
# 功能：为 CLI 命令统一创建平台 SDK 客户端、执行操作、打印 JSON，并保证关闭连接。
# 输入：fn 是接收 Client 的命令回调；平台地址、账号 Token、项目选择由 SDK 从环境变量读取。
# 输出/影响：返回 None，成功将回调结果打印到 stdout；错误输出 stderr 并退出码 1；finally 关闭 Client，实际请求副作用由 fn 决定。
def execute(fn):
    client = Client()
    try:
        typer.echo(json.dumps(fn(client), ensure_ascii=False, indent=2))
    except Exception as error:
        typer.echo(f"TestPilot 请求失败：{error}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()


# 功能：实现 testpilot status，查看平台健康状态而非启动 Agent 或测试器。
# 输入：无命令参数；平台地址与认证信息从 Client 环境配置读取。
# 输出/影响：返回 None；GET /api/health 的 JSON 由 execute 打印，不保存数据或更改平台状态。
@app.command()
def status():
    execute(lambda c: c.request("GET", "/api/health"))


# 功能：实现 testpilot environments，查看当前项目的测试目标环境配置。
# 输入：无命令参数；项目由 TESTPILOT_PROJECT_ID 等 SDK 配置选择。
# 输出/影响：返回 None；从 GET /api/state 提取 environments 并打印，不创建环境或启动请求测试。
@app.command("environments")
def environments():
    execute(lambda c: c.request("GET", "/api/state")["environments"])


# 功能：实现 testpilot case list，列出当前项目可执行用例。
# 输入：无命令参数；Client 携带当前账号与项目请求头。
# 输出/影响：返回 None；调用 cases_list 并打印 JSON，只查询 Case/版本摘要，不运行测试。
@cases.command("list")
def list_cases():
    execute(lambda c: c.tool("cases_list", {}))


# 功能：实现 testpilot projects，查看账号可访问的项目列表以便选择项目。
# 输入：无命令参数；Client 的账号 Token 与平台 URL 从环境变量读取。
# 输出/影响：返回 None；GET /api/state 后打印 projects；不直接改变 TESTPILOT_PROJECT_ID 或服务器项目配置。
@app.command("projects")
def projects():
    execute(lambda c:c.request("GET","/api/state")["projects"])


# 功能：实现 testpilot source list，按关键词和分页列出项目资料。
# 输入：query 是名称/解析正文关键词，空串列资料；offset/limit 为条目偏移与本页数量，由 Typer 从命令行解析。
# 输出/影响：返回 None；调用 sources_search 并打印结果，只读资料索引，不上传文件。
@sources.command("list")
def source_list(query: str = "", offset: int = 0, limit: int = 12):
    execute(lambda c: c.tool("sources_search", dict(query=query, offset=offset, limit=limit)))


# 功能：实现 testpilot source read，读取指定项目资料的一段解析正文。
# 输入：id 是 source Record ID；offset/limit 按字符选择片段；资料必须属于当前项目。
# 输出/影响：返回 None；调用 sources_read 并打印文本与 nextOffset，仅读平台资料，不执行文档中的指令。
@sources.command("read")
def source_read(id: str, offset: int = 0, limit: int = 8000):
    execute(lambda c: c.tool("sources_read", dict(id=id, offset=offset, limit=limit)))


# 功能：实现 testpilot source upload，将本机配套文档/资料上传到当前项目。
# 输入：file 是 Typer 解析的本机 Path；需存在、非空且最多 10 MB；文件名作请求参数，原始字节作请求体。
# 输出/影响：返回 None；POST /api/sources/upload 并打印记录，平台保存文件及解析文本；非法本地文件退出码 2，不把路径当服务端目录。
@sources.command("upload")
def source_upload(file: Path):
    if not file.is_file() or not 0 < file.stat().st_size <= 10 * 1024 * 1024:
        typer.echo("请提供非空文件，单个文件最多 10 MB", err=True)
        raise typer.Exit(2)
    execute(lambda c: c.request("POST", "/api/sources/upload", params={"filename": file.name},
                               content=file.read_bytes(), headers={"Content-Type": "application/octet-stream"}))


# 读取可视化步骤 JSON 并调用保存入口，产生可执行版本；导入动作本身不运行测试。
# 功能：实现 testpilot case steps，导入本机步骤 JSON 为可视化用例版本。
# 输入：file 是步骤列表 JSON 文件；name/kind 为名称和类型；case_id 空新建，否则修订；config 可选，读取运行配置 JSON。
# 输出/影响：返回 None；读取本机 JSON 并调用 cases_save_revision(steps=...)，平台保存不可变版本/生成资产，命令本身不执行测试。
@cases.command("steps")
def import_steps(file:Path,name:str,kind:str="api",case_id:str="",config:Path|None=None):
    execute(lambda c:c.tool("cases_save_revision",dict(name=name,kind=kind,steps=json.loads(file.read_text()),case_id=case_id,execution_config=json.loads(config.read_text()) if config else {})))


# 功能：实现 testpilot agent send，在已有对话继续补充要求而非另外创建单次任务。
# 输入：task_id 是 Agent 对话 ID；message 是本次用户正文，两者由命令行解析。
# 输出/影响：返回 None；POST 对话 messages 接口并打印结果，写 Task 消息并按业务逻辑安排后续执行，模型循环由绑定执行器负责。
@agent.command("send")
def send_message(task_id:str,message:str):
    execute(lambda c:c.request("POST","/api/tasks/"+task_id+"/messages",json={"content":message}))


# 功能：实现 testpilot case import，把本机原生测试源码及可选配套文件保存为正式版本。
# 输入：file 是源码 Path；name/kind 为名称/类型，case_id 决定新建/修订，entry 是相对入口名；config/files 分别为运行配置和文件内容映射 JSON。
# 输出/影响：返回 None；读本机文件后提交 cases_save_revision，平台保存 Case/版本记录与资产；不在 CLI 中编译并运行独立测试引擎。
@cases.command("import")
def import_case(file: Path, name: str, kind: str = "api", case_id: str = "", entry: str = "test_case.py", config: Path | None = None, files: Path | None = None):
    execute(lambda c: c.tool("cases_save_revision", dict(name=name, kind=kind, code=file.read_text(), case_id=case_id, entry=entry, config=json.loads(config.read_text()) if config else {}, files=json.loads(files.read_text()) if files else {})))


# 功能：实现 testpilot run start，对指定不可变用例版本提交一次测试执行。
# 输入：case_id/revision 定位版本；environment 选目标，timeout 为秒；key 为幂等标识；overrides 是配置覆盖 JSON 路径，network_capture 为 Web 捕获级别。
# 输出/影响：返回 None；调用 runs_start 并打印 Run ID/排队状态，平台写 Run/Job 后由执行器领取；提交成功不等于测试通过。
@runs.command("start")
def start(case_id: str, revision: int, environment: str = "", timeout: int = 120, key: str = "", overrides: Path | None = None, network_capture: str | None = None):
    execute(lambda c: c.tool("runs_start", dict(case_id=case_id, revision_number=revision, environment_id=environment, timeout=timeout, overrides=json.loads(overrides.read_text()) if overrides else {},network_capture=network_capture), key=key))


# 功能：实现 testpilot run get，查询执行状态和报告，按需短时间等待已有运行。
# 输入：id 是 Run ID；wait 是等待秒数，默认 0；统一参数校验限制最多 90 秒。
# 输出/影响：返回 None；调用 runs_get 并打印真实结果；只查询/等待，不启动或重复执行测试。
@runs.command("get")
def get(id: str, wait: int = 0):
    execute(lambda c: c.tool("runs_get", dict(id=id, wait_seconds=wait)))


# 功能：实现 testpilot run cancel，停止指定测试并保留已产生证据。
# 输入：id 是 Run ID，后端检查当前项目权限和运行状态。
# 输出/影响：返回 None；调用 runs_cancel 并打印更新结果，业务层改取消状态并处理作业/容器停止；CLI 不直接操作 Docker。
@runs.command("cancel")
def cancel(id: str):
    execute(lambda c: c.tool("runs_cancel", dict(id=id)))


# 功能：实现 testpilot agent start，创建可持续对话的 Agent 任务并指定权限策略。
# 输入：goal 是首次用户要求；environment 是目标环境 ID；network_capture 为记录级别；permission 是 Agent 的 auto/readonly/bypass 审批模式。
# 输出/影响：返回 None；POST /api/tasks 并打印 Task 信息，平台写对话/Job，由绑定执行器请求模型与调用工具；CLI 不运行模型循环。
@agent.command("start")
def task(goal: str, environment: str, network_capture: str | None = None, permission: str = "auto"):
    execute(lambda c: c.request("POST", "/api/tasks", json={"goal": goal, "environment_id": environment,"network_capture":network_capture,"permission_mode":permission}))


# 功能：实现 testpilot agent show，从当前项目状态中查找并显示一段已有对话。
# 输入：task_id 是待查看 Task ID；内部 lookup 使用 execute 创建的账号/项目 SDK 客户端。
# 输出/影响：返回 None；仅读 /api/state 并打印找到的 Task；找不到由 execute 输出错误及退出码 1，不修改对话。
@agent.command("show")
def show_task(task_id: str):
    # 功能：作为 show_task 的查询回调，在平台状态返回的任务列表中匹配指定对话。
    # 输入：client 是 execute 创建的 Client；task_id 来自外层 CLI 参数。
    # 输出/影响：返回匹配的 Task 字典；会 GET /api/state，找不到抛 ValueError；不启动 Agent 或写任务状态。
    def lookup(client):
        task = next((v for v in client.request("GET", "/api/state")["tasks"] if v["id"] == task_id), None)
        if not task: raise ValueError("当前项目中找不到该对话")
        return task
    execute(lookup)


# 功能：实现 testpilot agent permission，修改已有对话的工具审批模式。
# 输入：task_id 定位对话；mode 为 auto/readonly/bypass，含义是 Agent 操作审批策略而非登录角色。
# 输出/影响：返回 None；POST permissions 并打印 Task；后端保存模式、递增权限版本使旧授权失效，必要时重新排队恢复。
@agent.command("permission")
def task_permission(task_id: str, mode: str):
    execute(lambda c: c.request("POST", f"/api/tasks/{task_id}/permissions", json={"mode": mode}))


# 功能：实现 testpilot agent decide，提交用户对某次待批准工具调用的决定。
# 输入：task_id/approval_id 定位对话和审批；decision 为 approve/reject；reason 是可选人类理由。
# 输出/影响：返回 None；POST 审批接口并打印结果，平台保存决定并按暂停状态安排恢复，实际工具仍由执行器核验后执行。
@agent.command("decide")
def task_decision(task_id: str, approval_id: str, decision: str, reason: str = ""):
    execute(lambda c: c.request("POST", f"/api/tasks/{task_id}/approvals/{approval_id}", json={"decision": decision, "reason": reason}))


# 启动对外 MCP 协议服务，使 Codex 等外部 Agent 可复用同一套平台业务能力。
# 功能：实现 testpilot mcp serve，启动供外部 Agent 使用的 stdio MCP 服务。
# 输入：transport 默认 stdio；其他值不从 CLI 启动 HTTP 服务，因为 Streamable HTTP 已由平台 /mcp 提供。
# 输出/影响：正常由 server.run 持续处理 stdin/stdout 协议；非法模式输出说明并退出码 2；工具调用经 SDK 复用平台业务。
@mcp.command("serve")
def serve(transport: str = "stdio"):
    if transport != "stdio":
        typer.echo("Streamable HTTP 已由 API 的 /mcp 提供。stdio 桥使用此命令。", err=True)
        raise typer.Exit(2)
    from .mcp import server
    server.run(transport="stdio")


# 功能：实现 testpilot mobile devices，查看项目设备配置及占用情况。
# 输入：无命令参数；Client 的当前项目和账号限制可读设备范围。
# 输出/影响：返回 None；调用 devices_list 并打印列表；不连接手机或创建 Appium 会话。
@mobile.command("devices")
def mobile_devices():
    execute(lambda c: c.tool("devices_list", {}))

# 功能：实现 testpilot mobile applications，查看当前项目已上传的 Android 应用版本。
# 输入：无命令参数；平台 URL、身份与项目从 SDK 环境配置读取。
# 输出/影响：返回 None；调用 applications_list 并打印记录，不上传、安装或卸载 APK。
@mobile.command("applications")
def mobile_applications():
    execute(lambda c: c.tool("applications_list", {}))

# 功能：实现 testpilot mobile check，检查指定设备配置对应的 Appium/Android 是否连接正常。
# 输入：id 是项目 device Record ID，而非手机应用 ID 或探索会话 ID。
# 输出/影响：返回 None；调用 devices_check 并打印检查结果，执行服务可发实际连通性请求；不执行完整 APP 用例。
@mobile.command("check")
def mobile_check(id: str):
    execute(lambda c: c.tool("devices_check", {"id": id}))

# 功能：实现 testpilot mobile add-device，新增 Android 测试设备及对应服务地址。
# 输入：name 为展示名；udid 为 ADB 设备标识；url 是 Appium 地址，node_url 是可选设备节点 API 地址。
# 输出/影响：返回 None；POST /api/devices 并打印新设备记录，后端保存配置；此命令不安装 Appium/ADB 或自动创建模拟器。
@mobile.command("add-device")
def mobile_add_device(name: str, udid: str, url: str, node_url: str = ""):
    execute(lambda c: c.request("POST", "/api/devices", json=dict(name=name,udid=udid,appium_url=url,node_url=node_url)))

# 功能：实现 testpilot mobile upload，将本机 APK 上传为项目管理的 Android 应用版本。
# 输入：file 是本机 APK Path；name 为名称，package/activity/version 为可选包名、启动 Activity 和版本说明。
# 输出/影响：返回 None；读取 APK 字节并 POST /api/applications/upload，平台保存应用记录/文件；不立即在设备安装，错误由 execute 处理。
@mobile.command("upload")
def mobile_upload(file: Path, name: str, package: str = "", activity: str = "", version: str = ""):
    execute(lambda c: c.request("POST", "/api/applications/upload", params=dict(name=name,package=package,activity=activity,version=version), content=file.read_bytes(), headers={"Content-Type":"application/octet-stream"}))

# 功能：实现 testpilot mobile action，让用户或外部 Agent 进行受控 Android 探索操作。
# 输入：action 为 open/observe/click/input/swipe/back/close；session 续用会话，device/application 选目标，selector/by 定位，text/direction 提供动作值。
# 输出/影响：返回 None；调用 mobile_action 并打印结果；执行端可改变手机界面并保存截图/XML，close 释放会话；不将探索自动当正式测试通过。
@mobile.command("action")
def mobile_action(action: str, session: str = "", device: str = "", application: str = "", selector: str = "", by: str = "accessibility id", text: str = "", direction: str = "up"):
    execute(lambda c: c.tool("mobile_action", dict(action=action,session_id=session,device_id=device,app_id=application,selector=selector,by=by,text=text,direction=direction)))

# 功能：实现 testpilot performance plan，根据本机请求规格生成原生 JMeter 计划。
# 输入：requests 是本机 JSON 文件 Path，内容为请求字典列表，不是请求次数或并发用户数。
# 输出/影响：返回 None；读取 JSON 调用 performance_plan 并打印生成内容；不保存正式用例或启动压测，对目标没有负载。
@performance.command("plan")
def performance_plan(requests: Path):
    execute(lambda c: c.tool("performance_plan", {"requests": json.loads(requests.read_text())}))


# 功能：实现 testpilot network list，查询已有浏览器捕获会话或其中请求摘要。
# 输入：session_id 空列会话，非空指定捕获；query/errors_only 筛选，offset/limit 为条目分页。
# 输出/影响：返回 None；调用 network_list 并打印结果，只读授权会话记录；不开启全机抓包、不重放请求。
@network.command("list")
def network_list(session_id: str = "", query: str = "", errors_only: bool = False, offset: int = 0, limit: int = 20):
    execute(lambda c: c.tool("network_list", dict(session_id=session_id, query=query, errors_only=errors_only, offset=offset, limit=limit)))

# 功能：实现 testpilot network read，按需阅读某次捕获请求的元信息或正文片段。
# 输入：session_id/request_id 定位记录；section 选 metadata/request_body/response_body，offset/limit 为字符偏移和长度。
# 输出/影响：返回 None；调用 network_read 并打印片段/nextOffset；只读已保存内容，不请求目标接口或补抓旧正文。
@network.command("read")
def network_read(session_id: str, request_id: str, section: str = "metadata", offset: int = 0, limit: int = 8000):
    execute(lambda c: c.tool("network_read", dict(session_id=session_id, request_id=request_id, section=section, offset=offset, limit=limit)))

# 功能：实现 testpilot network capture，提交既有 Agent 对话的后续网络捕获策略调整。
# 输入：task_id 是对话 ID；level 为 off/api/detailed，reason 说明本次调整原因；以 task_id 关联统一工具分发。
# 输出/影响：返回 None；调用 network_capture_set 并打印真实处理结果；权限/用户显式关闭等限制由后端检查，变更仅影响未来请求。
@network.command("capture")
def network_setting(task_id: str, level: str, reason: str):
    execute(lambda c: c.tool("network_capture_set", dict(level=level, reason=reason), task_id=task_id))
