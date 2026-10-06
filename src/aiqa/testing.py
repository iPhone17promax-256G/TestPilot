# 核心业务层：用例版本、执行记录和对话状态都从这里组织。
# 阅读顺序建议 case_save → run_start → task_start/task_message，再看 runners 和 agent 的实际执行。

"""Shared business operations for the UI, Agent, CLI and MCP."""
import ast
import hashlib
import json
import os
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from urllib.parse import urlparse
from pathlib import Path
from .workspace import current_project
from .storage import assert_project
from .config import settings
from .assets import validate_assets, version_hash
from .performance import PerformanceConfig
from .assets import AppConfig
from .storage import Job, Mutation, Record, list_records, now, read_record, session, uid, update_record

TERMINAL = {"passed", "failed", "error", "cancelled", "timed_out", "interrupted", "completed"}


def stamp():
    return now().isoformat(timespec="seconds") + "Z"


# 所有业务文件都限制在 settings.data 下，先 resolve 再检查，防止 ../ 或链接逃逸。
# 本地执行子进程缺少某些平台文件时按需下载；平台数据库记录与磁盘正文是分开的。
def safe_path(relative, fetch=True):
    root = settings.data.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("路径超出工作区")
    if fetch and settings.role=="executor" and settings.executor_claim and not target.exists() and relative.startswith(("cases/","attachments/","sources/","applications/","apps/","runs/","inspections/")) and Path(relative).suffix:
        from .replica import download
        download(relative)
    return target


# 先写临时文件再替换目标，避免读到只写了一半的内容。
# 文件系统替换和数据库提交不是同一个事务，不能当成跨两种存储的原子提交。
def write_file(relative, content):
    target = safe_path(relative, fetch=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + uid("TMP"))
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, target)


def hydrate_case(payload):
    item = dict(payload)
    item["revisions"] = [dict(r, code=safe_path(r["path"]).read_text()) for r in item["revisions"]]
    return item


# Case 是用例身份，revision 是这个用例某一次保存的内容。
# 运行必须选定具体版本，后续编辑用例不会改变历史运行的输入。
def revision(case_id, number):
    item = read_record(case_id, "case")
    rev = next((r for r in item["revisions"] if r["number"] == number), None)
    if rev is None:
        raise ValueError("找不到用例版本")
    return item, rev


# 给可能被重复提交的写操作加幂等键：同键同参数取已有结果，同键不同参数拒绝。
# 数据库唯一约束处理并发撞键；没有传 key 的请求不会获得这项去重保证。
# 本地 HTTP 同步和外部工具副作用还有各自边界，不能把它概括成全系统“恰好执行一次”。
def mutate(scope, key, arguments, action):
    scope = current_project()+":"+scope
    fingerprint = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    def cached(db):
        row = db.scalar(select(Mutation).where(Mutation.scope == scope, Mutation.key == key))
        if row:
            if row.fingerprint != fingerprint:
                raise ValueError("幂等键已用于不同的请求内容")
            return row.result
    try:
        with session() as db:
            existing = cached(db) if key else None
            if existing is not None:
                return existing
            reservation = Mutation(scope=scope, key=key, fingerprint=fingerprint, result={}) if key else None
            if reservation:
                db.add(reservation)
                db.flush()
            result = action(db)
            if reservation:
                reservation.result = result
            return result
    except IntegrityError:
        if not key:
            raise
        with session() as db:
            existing = cached(db)
            if existing is None:
                raise
            return existing


def validate_code(code):
    if not code.strip() or len(code) > 150_000:
        raise ValueError("代码不能为空，且不能超过 150KB")
    ast.parse(code, filename="test_case.py")


# 保存入口同时服务人和 Agent。steps 会先校验并编译成 Python/JMX，然后才生成版本。
# 每个版本保存代码、辅助文件、配置、步骤及 stepMap；内容摘要用于执行前核对。
# Agent 调用还有可视化优先检查，不能仅凭模型说“脚本更方便”就绕过。
def case_save(name, kind, code, description="", case_id="", entry="test_case.py", source="手工", task_id="", key="", files=None, config=None, steps=None, fallback=None):
    files, config = files or {}, config or {}
    mapping = []
    if task_id:
        from .authoring import native_fallback, visual_authoring
        task = read_record(task_id,"task")
        if steps is None:
            fallback = native_fallback(kind, code, fallback, task)
        else:
            visual_authoring(steps)
    # 可视化保存以步骤为输入，生成脚本是派生结果；不需要 Agent 同时维护两份独立内容。
    if steps is not None:
        from .steps import compile_steps_details
        steps,code,mapping=compile_steps_details(kind,steps)
        from .step_blocks import active_step_indices
        active = active_step_indices(steps)
        for index,step in enumerate(steps):
            if index not in active: continue
            required = [step["value"]] if step["action"] in {"csv","upload"} else list(step["uploads"].values()) if step["body_type"]=="multipart" else []
            for filename in required:
                if filename not in files: raise ValueError("请在辅助文件 files 中提供 "+filename)
    entry, config = validate_assets(kind, code, entry, files, config)
    if not name.strip() or len(name) > 120:
        raise ValueError("需要有效的用例名称")
    if case_id:read_record(case_id,"case")
    if task_id:read_record(task_id,"task")
    args = dict(name=name, kind=kind, code=code, description=description, case_id=case_id, entry=entry, task_id=task_id, files=files, config=config, steps=steps, fallback=fallback)
    # 锁定用例，比较新旧内容；完全相同就复用版本，有变化才追加版本号。
    # 版本目录记录执行输入，Record 保存它的路径及元信息；返回时 hydrate_case 才读出源码。
    def action(db):
        row = db.scalar(select(Record).where(Record.id == case_id, Record.kind == "case").with_for_update()) if case_id else None
        if case_id and not row:
            raise ValueError("用例不存在")
        if row: assert_project(row)
        payload = dict(row.payload) if row else {"id": uid("TC"), "name": name.strip(), "kind": kind, "description": description, "filename": "plan.jmx" if kind == "performance" else "test_case.py", "source": source, "tags": [], "revisions": []}
        if payload["kind"] != kind:
            raise ValueError("已有用例的执行类型不能修改，请创建另一条用例")
        payload.update(name=name.strip(), description=description)
        code_hash = version_hash(code, files, config)
        unchanged = payload["revisions"] and payload["revisions"][-1]["hash"] == code_hash and payload["revisions"][-1]["entry"] == entry and payload["revisions"][-1].get("steps") == steps and payload["revisions"][-1].get("fallback") == fallback
        if not unchanged:
            number = len(payload["revisions"]) + 1
            path = f"cases/{payload['id']}/{number}/{payload['filename']}"
            write_file(path, code)
            for filename, content in files.items():
                write_file(f"cases/{payload['id']}/{number}/{filename}", content)
            payload["revisions"] = [*payload["revisions"], {"number": number, "hash": code_hash, "path": path, "entry": entry, "files": files, "config": config, "mode": "steps" if steps is not None else "script", "steps": steps, "stepMap": mapping, "fallback": fallback if steps is None else None, "createdAt": stamp()}]
        if row:
            row.payload = payload
        else:
            db.add(Record(id=payload["id"], kind="case", payload=payload))
        if task_id:
            task = db.scalar(select(Record).where(Record.id == task_id).with_for_update())
            if task:
                task.payload = dict(task.payload, caseIds=list(dict.fromkeys([*task.payload.get("caseIds", []), payload["id"]])))
        return payload
    return hydrate_case(mutate("cases_save", key, args, action))


def environment_save(name, url, execution_url="", description="", environment_id=""):
    for value in [url, execution_url or url]:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("环境地址必须为不带账号密码的 HTTP/HTTPS URL")
    if not name.strip():
        raise ValueError("环境名称不能为空")
    payload = {"id": environment_id or uid("ENV"), "name": name.strip(), "url": url.rstrip("/"), "executionUrl": (execution_url or url).rstrip("/"), "description": description}
    with session() as db:
        row = db.get(Record, payload["id"])
        if row:
            assert_project(row)
            if row.kind != "environment":
                raise ValueError("环境 ID 不正确")
            row.payload = payload
        else:
            db.add(Record(id=payload["id"], kind="environment", payload=payload))
    return payload


# “启动”在业务层的含义是创建 Run 和 Job；真正启动容器发生在 execute_run。
# 此时固定版本号、代码摘要、环境和执行配置快照，确保报告可追溯到当时的输入。
# APP 额外保存应用/设备快照；测试类型不同，但都返回统一的 Run 记录。
def run_start(case_id, revision_number, environment_id, task_id="", timeout=120, key="", overrides=None, network_capture=None):
    if task_id:read_record(task_id,"task")
    item, rev = revision(case_id, revision_number)
    env = read_record(environment_id, "environment")
    config = dict(rev.get("config", {}), **(overrides or {}))
    if item["kind"] == "performance":
        config = PerformanceConfig.model_validate(config).model_dump()
        seconds = sum(x["seconds"] for x in config["stages"]) if config["stages"] else config["duration_seconds"]
        timeout = max(seconds + 90, int(timeout))
    elif item["kind"] == "app":
        config = AppConfig.model_validate(config).model_dump()
        if not config["app_id"] or not config["device_id"]:raise ValueError("APP 运行需要选择应用版本和测试设备")
        read_record(config["app_id"], "application")
        read_record(config["device_id"], "device")
    elif overrides:
        raise ValueError("API/Web 用例没有运行配置覆盖项")
    timeout = max(5, min(int(timeout), 3600 if item["kind"] == "performance" else 600))
    from .network import resolve_level
    capture_level = resolve_level(task_id, network_capture) if item["kind"] == "web" else None
    args = dict(case_id=case_id, revision_number=revision_number, environment_id=environment_id, task_id=task_id, timeout=timeout, config=config, network_capture=capture_level)
    def action(db):
        identity = uid("RUN")
        payload = {"id": identity, "caseId": case_id, "revision": revision_number, "status": "queued", "duration": "—", "createdAt": stamp(), "environment": env["name"], "environmentSnapshot": env, "taskId": task_id, "demo": False, "timeout": timeout, "artifacts": [], "items": [], "summary": "等待执行进程领取", "codeHash": rev["hash"], "kind": item["kind"], "configSnapshot": config}
        if settings.role=="platform":
            from .executors import selected_executor
            payload["executorId"]=read_record(task_id).get("executorId","") if task_id else selected_executor()
            if not payload["executorId"]:raise ValueError("请先连接本地执行器")
        elif task_id:payload["executorId"]=read_record(task_id).get("executorId","")
        if item["kind"] == "web":
            payload["networkCaptureLevel"] = capture_level
        if item["kind"] == "app":
            payload["appSnapshot"] = read_record(config["app_id"], "application")
            payload["deviceSnapshot"] = read_record(config["device_id"], "device")
        db.add(Record(id=identity, kind="run", status="queued", payload=payload))
        db.add(Job(id=uid("JOB"), kind="test", record_id=identity))
        if task_id:
            task = db.scalar(select(Record).where(Record.id == task_id).with_for_update())
            if task:
                task.payload = dict(task.payload, runIds=[*task.payload.get("runIds", []), identity])
        return payload
    return mutate("runs_start", key, args, action)


# 先把取消意图写入状态，执行器的轮询再停止实际容器；不是本 HTTP 线程直接杀容器。
def run_cancel(run_id):
    read_record(run_id,"run")
    return update_record(run_id, lambda p: p.update(status="cancelled", summary="用户取消了本次运行") if p["status"] not in TERMINAL else None)


# Task 是一段 Agent 对话的持久记录，包含用户消息、工具事件、用例和执行关联。
# 首条消息与模型选择一起保存，再创建 agent 类型 Job 让本地执行器领取。
def task_start(goal, environment_id, key="", attachment_ids=None, network_capture=None, permission_mode="auto", model_profile_id="", reasoning_effort="", executor_id=""):
    from .attachments import attachment_refs
    from .agent.permissions import validate_mode
    validate_mode(permission_mode)
    from .network_capture import validate_level
    if network_capture is not None: validate_level(network_capture)
    attachments = attachment_refs(attachment_ids or [])
    goal = goal.strip() or ("请阅读附件，概括需求与待确认项，先不要执行测试。" if attachments else "")
    env = read_record(environment_id, "environment")
    if not goal.strip() or len(goal) > 6000:
        raise ValueError("请提供测试目标，最多 6000 字")
    selection = {}
    if settings.role == "platform":
        from .models import validate_selection
        from .executors import selected_executor
        chosen = validate_selection(model_profile_id, reasoning_effort)
        executor_id = selected_executor(executor_id)
        if not executor_id: raise ValueError("请先连接本地执行器")
        selection = dict(modelProfileId=chosen["id"], reasoningEffort=reasoning_effort, executorId=executor_id)
    elif not settings.model_key or not settings.model_url:
        raise ValueError("本地执行器没有配置模型 API 地址和密钥")
    def action(db):
        identity = uid("TASK")
        payload = {"id": identity, "title": goal[:40], "goal": goal, "status": "queued", "step": 0, "caseIds": [], "runIds": [], "events": [], "inspections": [], "findings": [], "environmentId": environment_id, "environment": env["name"], "createdAt": stamp(), "summary": "等待 Agent 领取", "rounds": 0, "messages": [{"id": uid("MSG"), "role": "user", "content": goal, "attachments": attachments, "captureLevel": network_capture, "createdAt": stamp(), "delivered": False}]}
        payload.update(selection)
        payload["messages"][0].update({k:v for k,v in selection.items() if k!="executorId"})
        payload.update(permissionMode=permission_mode, permissionVersion=0, intentVersion=0, approvals=[])
        db.add(Record(id=identity, kind="task", status="queued", payload=payload))
        db.add(Job(id=uid("JOB"), kind="agent", record_id=identity))
        return payload
    return mutate("tasks_start", key, dict(goal=goal, environment_id=environment_id, attachment_ids=attachment_ids or [], network_capture=network_capture, permission_mode=permission_mode, selection=selection), action)


# 取消对话时让旧审批失效，并请求取消关联测试；旧操作不能在用户停止后重新获批。
def task_cancel(task_id):
    read_record(task_id,"task")
    def cancel(p):
        if p["status"] in {"queued","running","waiting_approval","cancelled"}:
            from .agent.permissions import invalidate
            invalidate(p,"用户停止了对话，旧操作不再有效")
            p.update(status="cancelled",summary="用户取消，已有用例和结果已保留")
            p["messages"]=[dict(m,delivered=True,suppressed=True) if m["role"]=="user" and not m.get("delivered") else m for m in p.get("messages",[])]
    task=update_record(task_id,cancel)
    if task["status"]=="cancelled":
        for run_id in task.get("runIds",[]):run_cancel(run_id)
    return task


def task_resume(task_id):
    read_record(task_id,"task")
    with session() as db:
        job = db.scalar(select(Job).where(Job.record_id == task_id).with_for_update())
        row = db.scalar(select(Record).where(Record.id == task_id, Record.kind == "task").with_for_update())
        if not row or row.status not in {"interrupted", "error", "budget_exhausted", "needs_input"}:
            raise ValueError("只有中断、错误、等待继续或预算耗尽的任务可继续")
        row.payload = dict(row.payload, status="queued", summary="从持久化状态继续")
        row.status = "queued"
        job = db.scalar(select(Job).where(Job.record_id == task_id).with_for_update())
        job.status, job.claim, job.lease = "queued", "", None
    return read_record(task_id)


def task_messages(task_id):
    task = read_record(task_id, "task")
    if task.get("messages") is not None:return task["messages"]
    legacy = [{"id": task_id+"-goal", "role":"user", "content":task["goal"], "createdAt":task["createdAt"], "delivered":True}]
    legacy += [{"id":e["id"], "role":"tool", "eventId":e["id"], "createdAt":e["createdAt"]} for e in task.get("events",[])]
    if task["status"] not in {"running","queued"}:
        legacy.append({"id":task_id+"-summary", "role":"assistant", "content":task["summary"], "createdAt":task["createdAt"]})
    return legacy


# 同一 Task 中追加用户消息。新要求会增加 intentVersion，使旧意图下的审批失效。
# 正在执行时先保存消息，下一次决策或后续领取再消费；不会强行修改一次在途模型请求。
def task_message(task_id, content, key="", attachment_ids=None, network_capture=None, model_profile_id="", reasoning_effort=""):
    from .attachments import attachment_refs
    from .network_capture import validate_level
    if network_capture is not None: validate_level(network_capture)
    attachments = attachment_refs(attachment_ids or [])
    content = content.strip() or ("请阅读附件，概括需求与待确认项，先不要执行测试。" if attachments else "")
    if not content.strip() or len(content)>6000:raise ValueError("消息需要 1～6000 字")
    existing=task_messages(task_id)
    selection={}
    if settings.role=="platform":
        from .models import validate_selection
        prior=read_record(task_id,"task")
        chosen=validate_selection(model_profile_id or prior.get("modelProfileId",""),reasoning_effort)
        selection=dict(modelProfileId=chosen["id"],reasoningEffort=reasoning_effort)
    def action(db):
        job=db.scalar(select(Job).where(Job.record_id==task_id).with_for_update())
        row=db.scalar(select(Record).where(Record.id==task_id,Record.kind=="task").with_for_update())
        if not row:raise ValueError("对话不存在")
        assert_project(row)
        p=dict(row.payload)
        from .agent.permissions import invalidate
        p["intentVersion"]=p.get("intentVersion",0)+1
        invalidate(p,"用户发送了新要求，不执行旧操作")
        p["messages"]=[*p.get("messages",existing), {"id":uid("MSG"),"role":"user","content":content.strip(),"attachments":attachments,"captureLevel":network_capture,"createdAt":stamp(),"delivered":False}]
        p["messages"][-1].update(selection)
        if row.status=="waiting_approval":p.update(status="queued",resumeRequested=True,summary="按新要求继续，旧操作已取消");row.status="queued"
        if not job or job.status!="running":
            p.update(status="queued",summary="等待 Agent 回复")
            row.status="queued"
            if job:job.status,job.claim,job.lease="queued","",None
            else:db.add(Job(id=uid("JOB"),kind="agent",record_id=task_id))
        row.payload=p
        return p
    return mutate("task_message",key,dict(task_id=task_id,content=content,attachment_ids=attachment_ids or [],network_capture=network_capture,selection=selection),action)


# 只消费当前子进程绑定模型的连续消息；遇到模型不同的消息就留给后续进程。
# 例如 A→B→A 三条消息不能跳过 B 提前消费最后一条 A，必须保持用户发言顺序。
def take_user_messages(task_id):
    taken=[]
    def change(p):
        messages=[]
        deferred=False
        for original in p.get("messages",[]):
            message=dict(original)
            chosen=__import__("aiqa.models",fromlist=["active_selection"]).active_selection
            if message["role"]=="user" and not message.get("delivered") and not message.get("suppressed"):
                if chosen and (message.get("modelProfileId"),message.get("reasoningEffort",""))!=chosen:deferred=True
                if not deferred:
                    taken.append(message.copy());message["delivered"]=True
            messages.append(message)
        p["messages"]=messages
    update_record(task_id,change)
    return taken


# 流式输出会反复更新同一消息 ID，而不是每个字符追加一条聊天消息。
# 最终消息写入后，迟到的部分片段不能覆盖已经完整的内容。
def conversation_append(task_id, role, content="", identity="", event_id="", partial=False):
    def change(p):
        messages=p.get("messages",[])
        identity_value=identity or uid("MSG")
        messages=[dict(m) for m in messages]
        existing=next((m for m in messages if m["id"]==identity_value),None)
        if existing:
            if not partial or existing.get("streaming"):
                existing.update(content=content,streaming=partial)
            p["messages"]=messages
        else:p["messages"]=[*messages,dict(id=identity_value,role=role,content=content,eventId=event_id,streaming=partial,createdAt=stamp())]
    update_record(task_id,change)


# 工具 call_id 对应一次调用；planned/running/completed 等状态更新同一条事件。
# 聊天中的 tool 消息通过 eventId 指向事件，界面可展开看到参数与真实结果。
def task_event(task_id, name, arguments, result, call_id="", status="completed"):
    identity=uid("EVENT")
    def change(p):
        events=[dict(e) for e in p.get("events",[])]
        existing=next((e for e in events if call_id and e.get("callId")==call_id),None)
        if existing:existing.update(result=result,status=status)
        else:events.append(dict(id=identity,callId=call_id,name=name,arguments=arguments,result=result,status=status,createdAt=stamp()))
        p["events"]=events;p["step"]=len(events)
        if not existing:
            p["messages"]=[*p.get("messages",[]),dict(id=identity,role="tool",eventId=identity,createdAt=stamp())]
    update_record(task_id,change)



# 给 Agent 项目概况、环境和资料目录，不一次塞入所有文件正文。
# 模型需要更详细的需求时通过 sources_read/attachments_read 按需读取。
def project_context(environment_id=""):
    from .sources import source_catalog
    return {"project": project_settings()["name"], "environment": read_record(environment_id, "environment") if environment_id else None, **source_catalog(), "credentials": "读取 TEST_USERNAME / TEST_PASSWORD 环境变量；不要在源码或工具参数中填写密码。", "applications": list_records("application"), "devices": __import__("aiqa.mobile", fromlist=["devices_list"]).devices_list(), "capabilities": ["api", "web", "performance (JMeter)", "app (Android/Appium)"], "principles": "业务预期来自需求；发现错误不能修改预期来掩盖。资料缺失时记录待确认项。"}


def project_settings():
    try:
        return read_record(current_project(), "project")
    except ValueError:
        return {"name": "MM 平台化", "description": "MM 登录、鉴权和协作功能的被测项目。"}


def model_settings():
    if settings.role=="executor":
        from .models import active_profile
        if active_profile:return dict(active_profile,configured=bool(settings.model_key))
    if settings.role=="platform":
        from .executors import profiles
        rows=profiles()
        chosen=next((r for r in rows if r.get("default")),rows[0] if rows else {})
        return dict(model=chosen.get("model",""),baseUrl=chosen.get("baseUrl",""),maxSteps=chosen.get("maxSteps",24),configured=bool(rows))
    configs = list_records("settings")
    saved = configs[0] if configs else {}
    return dict(saved, model=saved.get("model") or settings.model, baseUrl=saved.get("baseUrl") or settings.model_url, maxSteps=saved.get("maxSteps") or settings.max_steps, configured=bool(settings.model_key and settings.model_url))


def state_snapshot():
    from .sources import sources_list
    return {"cases": [hydrate_case(c) for c in list_records("case")], "runs": list_records("run"), "tasks": [dict(v,messages=task_messages(v["id"])) for v in list_records("task")], "environments": list_records("environment"), "projectId": current_project(), "projects": list_records("project"), "projectName": project_settings()["name"], "projectDescription": project_settings()["description"], "sources": sources_list(), "settings": model_settings(), "applications": list_records("application"), "devices": __import__("aiqa.mobile", fromlist=["devices_list"]).devices_list()}


def project_save(name, description="", project_id=""):
    if not name.strip():raise ValueError("项目名称不能为空")
    identity = project_id or uid("PROJECT")
    with session() as db:
        row = db.get(Record, identity)
        payload = dict(id=identity, name=name.strip(), description=description)
        if row and row.kind != "project":raise ValueError("项目 ID 不正确")
        if row:
            from .storage import assert_project
            assert_project(row)
            payload.update({k:v for k,v in row.payload.items() if k in {"ownerId","members"}})
        else:
            from .workspace import current_user
            if current_user() != "service":payload.update(ownerId=current_user(),members=[])
        if row:row.payload=payload
        else:db.add(Record(id=identity,kind="project",payload=payload))
    return payload


# 首次空项目的 MM 演示资料从这里写入，包括健康接口和登录约定。
# 因此 Agent 知道 MM 接口有明确资料来源；不能推广为它自动知道任意项目的业务。
def seed():
    if not list_records("project"):
        project_save("MM 平台化", "MM 登录、鉴权和协作功能的被测项目。", "PROJECT")
    if not list_records("environment"):
        environment_save("MM 本地环境", "http://127.0.0.1:8065", "http://mm-target:8065", "浏览器地址与容器执行地址分别配置")
    if not list_records("source"):
        with session() as db:
            db.add(Record(id="SRC-MM", kind="source", payload={"id": "SRC-MM", "name": "MM 登录与健康检查 API", "content": "GET /api/v4/system/ping：HTTP 200，JSON status 为 OK。POST /api/v4/users/login：JSON login_id、password；不存在账号应被拒绝，HTTP 401，body 包含 id/message/status_code。有效账号登录成功返回用户信息，响应头 Token 用于 Authorization: Bearer <token>。UI /login 账号和密码输入框，登录按钮；错误账号应提示错误且不能进入工作区。错误提示的精确文案未在需求中约定，不应臆造。"}))
