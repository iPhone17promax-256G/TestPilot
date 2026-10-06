# Typer 命令行入口：aiqa 子命令将参数转换为 HTTP 请求，不在 CLI 里另写执行引擎。
# CLI 是外部 Agent/人可用的调用方式；实际测试仍由已连接的本地执行器领取。

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
def execute(fn):
    client = Client()
    try:
        typer.echo(json.dumps(fn(client), ensure_ascii=False, indent=2))
    except Exception as error:
        typer.echo(f"AIQA 请求失败：{error}", err=True)
        raise typer.Exit(1)
    finally:
        client.close()


@app.command()
def status():
    execute(lambda c: c.request("GET", "/api/health"))


@app.command("environments")
def environments():
    execute(lambda c: c.request("GET", "/api/state")["environments"])


@cases.command("list")
def list_cases():
    execute(lambda c: c.tool("cases_list", {}))


@app.command("projects")
def projects():
    execute(lambda c:c.request("GET","/api/state")["projects"])


@sources.command("list")
def source_list(query: str = "", offset: int = 0, limit: int = 12):
    execute(lambda c: c.tool("sources_search", dict(query=query, offset=offset, limit=limit)))


@sources.command("read")
def source_read(id: str, offset: int = 0, limit: int = 8000):
    execute(lambda c: c.tool("sources_read", dict(id=id, offset=offset, limit=limit)))


@sources.command("upload")
def source_upload(file: Path):
    if not file.is_file() or not 0 < file.stat().st_size <= 10 * 1024 * 1024:
        typer.echo("请提供非空文件，单个文件最多 10 MB", err=True)
        raise typer.Exit(2)
    execute(lambda c: c.request("POST", "/api/sources/upload", params={"filename": file.name},
                               content=file.read_bytes(), headers={"Content-Type": "application/octet-stream"}))


# 读取可视化步骤 JSON 并调用保存入口，产生可执行版本；导入动作本身不运行测试。
@cases.command("steps")
def import_steps(file:Path,name:str,kind:str="api",case_id:str="",config:Path|None=None):
    execute(lambda c:c.tool("cases_save_revision",dict(name=name,kind=kind,steps=json.loads(file.read_text()),case_id=case_id,execution_config=json.loads(config.read_text()) if config else {})))


@agent.command("send")
def send_message(task_id:str,message:str):
    execute(lambda c:c.request("POST","/api/tasks/"+task_id+"/messages",json={"content":message}))


@cases.command("import")
def import_case(file: Path, name: str, kind: str = "api", case_id: str = "", entry: str = "test_case.py", config: Path | None = None, files: Path | None = None):
    execute(lambda c: c.tool("cases_save_revision", dict(name=name, kind=kind, code=file.read_text(), case_id=case_id, entry=entry, config=json.loads(config.read_text()) if config else {}, files=json.loads(files.read_text()) if files else {})))


@runs.command("start")
def start(case_id: str, revision: int, environment: str = "", timeout: int = 120, key: str = "", overrides: Path | None = None, network_capture: str | None = None):
    execute(lambda c: c.tool("runs_start", dict(case_id=case_id, revision_number=revision, environment_id=environment, timeout=timeout, overrides=json.loads(overrides.read_text()) if overrides else {},network_capture=network_capture), key=key))


@runs.command("get")
def get(id: str, wait: int = 0):
    execute(lambda c: c.tool("runs_get", dict(id=id, wait_seconds=wait)))


@runs.command("cancel")
def cancel(id: str):
    execute(lambda c: c.tool("runs_cancel", dict(id=id)))


@agent.command("start")
def task(goal: str, environment: str, network_capture: str | None = None, permission: str = "auto"):
    execute(lambda c: c.request("POST", "/api/tasks", json={"goal": goal, "environment_id": environment,"network_capture":network_capture,"permission_mode":permission}))


@agent.command("show")
def show_task(task_id: str):
    def lookup(client):
        task = next((v for v in client.request("GET", "/api/state")["tasks"] if v["id"] == task_id), None)
        if not task: raise ValueError("当前项目中找不到该对话")
        return task
    execute(lookup)


@agent.command("permission")
def task_permission(task_id: str, mode: str):
    execute(lambda c: c.request("POST", f"/api/tasks/{task_id}/permissions", json={"mode": mode}))


@agent.command("decide")
def task_decision(task_id: str, approval_id: str, decision: str, reason: str = ""):
    execute(lambda c: c.request("POST", f"/api/tasks/{task_id}/approvals/{approval_id}", json={"decision": decision, "reason": reason}))


# 启动对外 MCP 协议服务，使 Codex 等外部 Agent 可复用同一套平台业务能力。
@mcp.command("serve")
def serve(transport: str = "stdio"):
    if transport != "stdio":
        typer.echo("Streamable HTTP 已由 API 的 /mcp 提供。stdio 桥使用此命令。", err=True)
        raise typer.Exit(2)
    from .mcp import server
    server.run(transport="stdio")


@mobile.command("devices")
def mobile_devices():
    execute(lambda c: c.tool("devices_list", {}))

@mobile.command("applications")
def mobile_applications():
    execute(lambda c: c.tool("applications_list", {}))

@mobile.command("check")
def mobile_check(id: str):
    execute(lambda c: c.tool("devices_check", {"id": id}))

@mobile.command("add-device")
def mobile_add_device(name: str, udid: str, url: str, node_url: str = ""):
    execute(lambda c: c.request("POST", "/api/devices", json=dict(name=name,udid=udid,appium_url=url,node_url=node_url)))

@mobile.command("upload")
def mobile_upload(file: Path, name: str, package: str = "", activity: str = "", version: str = ""):
    execute(lambda c: c.request("POST", "/api/applications/upload", params=dict(name=name,package=package,activity=activity,version=version), content=file.read_bytes(), headers={"Content-Type":"application/octet-stream"}))

@mobile.command("action")
def mobile_action(action: str, session: str = "", device: str = "", application: str = "", selector: str = "", by: str = "accessibility id", text: str = "", direction: str = "up"):
    execute(lambda c: c.tool("mobile_action", dict(action=action,session_id=session,device_id=device,app_id=application,selector=selector,by=by,text=text,direction=direction)))

@performance.command("plan")
def performance_plan(requests: Path):
    execute(lambda c: c.tool("performance_plan", {"requests": json.loads(requests.read_text())}))


@network.command("list")
def network_list(session_id: str = "", query: str = "", errors_only: bool = False, offset: int = 0, limit: int = 20):
    execute(lambda c: c.tool("network_list", dict(session_id=session_id, query=query, errors_only=errors_only, offset=offset, limit=limit)))

@network.command("read")
def network_read(session_id: str, request_id: str, section: str = "metadata", offset: int = 0, limit: int = 8000):
    execute(lambda c: c.tool("network_read", dict(session_id=session_id, request_id=request_id, section=section, offset=offset, limit=limit)))

@network.command("capture")
def network_setting(task_id: str, level: str, reason: str):
    execute(lambda c: c.tool("network_capture_set", dict(level=level, reason=reason), task_id=task_id))
