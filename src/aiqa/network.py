# 浏览器网络证据的业务层：定义当前轮次级别、记录归属、列表和分页读取。
# 采集与给模型读取分开；保存了请求并不代表正文已自动加入模型上下文。

"""Session ownership and paged access to passive browser network evidence."""
import json
import re
from .network_capture import validate_level, MAX_TOTAL_BYTES
from .storage import Record, session, uid, read_record, list_records, update_record
from .testing import safe_path, stamp
from .workspace import current_user


# 网络级别按最新用户消息分轮次，默认 api；用户本轮明确关闭后锁定为 off。
def task_policy(task_id):
    task = read_record(task_id, "task")
    latest = next((m for m in reversed(task.get("messages", [])) if m["role"] == "user" and not m.get("suppressed")), {})
    turn = latest.get("id", task_id)
    policy = task.get("networkCapture", {})
    if policy.get("turnId") != turn:
        level = validate_level(latest.get("captureLevel") or "api")
        policy = dict(level=level, locked=level == "off", turnId=turn, reason="本轮默认设置")
        update_record(task_id, lambda p: p.update(networkCapture=policy))
    return policy


# Agent 可调整后续操作的采集级别，但不能重新开启用户已关闭的一轮。
# 改变级别不会补回之前没采集的正文，需要重新执行相关浏览器操作才能获得新证据。
def capture_set(task_id, level, reason):
    if not task_id:
        raise ValueError("调整网络记录需要当前 Agent task_id")
    level = validate_level(level)
    policy = task_policy(task_id)
    if policy["locked"] and level != "off":
        raise ValueError("用户已关闭本轮网络记录，Agent 不能重新开启")
    policy = dict(policy, level=level, reason=reason)
    update_record(task_id, lambda p: p.update(networkCapture=policy))
    return dict(policy, appliesTo="本轮后续 browser_explore 和 Web 运行，已结束的采集不受影响；下一轮用户消息恢复默认",
                message="采集与读取分开：通过 network_list/network_read 按需查看；升级后需要重新执行相关操作")


def resolve_level(task_id="", requested=None):
    policy = task_policy(task_id) if task_id else {"level": "api", "locked": False}
    if policy["locked"]:
        return "off"
    return validate_level(requested or policy["level"])


def create_session(owner_id, kind, relative, target_url, level, task_id=""):
    if kind not in {"run", "inspection"}:
        raise ValueError("不支持的网络会话类型")
    read_record(task_id, "task") if task_id else read_record(owner_id, "run")
    identity = uid("NET")
    payload = dict(id=identity, ownerId=owner_id, accountId=current_user(), kind=kind, taskId=task_id,
                   path=relative + "/network", targetUrl=target_url, level=validate_level(level),
                   status="queued", count=0, createdAt=stamp(), bytes=0, dropped=0, filtered=0)
    with session() as db:
        db.add(Record(id=identity, kind="network_session", status="queued", payload=payload))
    return payload


# 检查网络会话所属项目/账号及 Task；知道 NET ID 不代表有权读取另一段对话的网络包。
def owned_session(identity, task_id=""):
    capture = read_record(identity, "network_session")
    if task_id and capture.get("taskId") != task_id:
        raise ValueError("当前会话中找不到该网络记录")
    owner = read_record(capture["ownerId"] if capture["kind"] == "run" else capture["taskId"],
                        "run" if capture["kind"] == "run" else "task")
    return capture


def _json_file(capture, filename, maximum):
    # The owner was checked before resolving files; links cannot escape it.
    folder = safe_path(capture["path"])
    target = safe_path(capture["path"] + "/" + filename)
    from .config import settings
    expected = settings.data.resolve() / capture["path"]
    if folder != expected or target.parent != folder:
        raise ValueError("网络记录路径超出当前会话")
    if not target.is_file():
        return None
    if target.stat().st_size > maximum:
        raise ValueError("网络记录超过读取上限")
    return json.loads(target.read_text(encoding="utf-8"))


def session_summary(identity, task_id="", finish=False):
    capture = owned_session(identity, task_id)
    if capture["status"] in {"completed", "interrupted", "error"} and not finish:
        return {k: v for k, v in capture.items() if k != "path"}
    index = _json_file(capture, "index.json", MAX_TOTAL_BYTES)
    if index:
        capture.update(status=index["status"], count=len(index["requests"]),
                       bytes=index["bytes"], dropped=index["dropped"], filtered=index["filtered"],
                       error=index.get("error", ""), limits=index["limits"],
                       failed=sum(r["state"] == "failed" or (r["status"] or 0) >= 400 for r in index["requests"]))
        if finish and index["status"] != "completed":
            capture.update(status="interrupted", error=index.get("error") or "采集未正常结束，保留已有请求记录")
    elif finish:
        capture.update(status="error", error="执行结束但未产出网络记录")
    if finish:
        update_record(identity, lambda p: p.update(capture))
    return {k: v for k, v in capture.items() if k != "path"}


def network_list(session_id="", query="", errors_only=False, offset=0, limit=20, task_id=""):
    if not session_id:
        sessions = [r for r in list_records("network_session") if not task_id or r.get("taskId") == task_id]
        return dict(sessions=[session_summary(r["id"], task_id) for r in sessions[offset:offset + limit]],
                    total=len(sessions), nextOffset=offset + limit if offset + limit < len(sessions) else None)
    capture = owned_session(session_id, task_id)
    index = _json_file(capture, "index.json", MAX_TOTAL_BYTES) or {"requests": []}
    rows = [r for r in index["requests"] if query.lower() in (r["method"] + " " + r["url"]).lower()
            and (not errors_only or r["state"] == "failed" or (r["status"] or 0) >= 400)]
    end = offset + limit
    return dict(session=session_summary(session_id, task_id), requests=rows[offset:end], total=len(rows),
                nextOffset=end if end < len(rows) else None)


# metadata 与请求/响应正文分开读取，正文支持分页；模型按需调查，不把全部包一次返回。
def network_read(session_id, request_id, section="metadata", offset=0, limit=8000, task_id=""):
    if not re.fullmatch(r"REQ-[0-9]{4}", request_id):
        raise ValueError("请求编号不合法")
    capture = owned_session(session_id, task_id)
    record = _json_file(capture, request_id + ".json", 700_000)
    if record is None:
        raise ValueError("找不到该请求详情，可能达到采集上限")
    if section == "metadata":
        return {k: v for k, v in record.items() if k not in {"requestBody", "responseBody"}}
    if section not in {"request_body", "response_body"}:
        raise ValueError("读取部分必须为 metadata、request_body 或 response_body")
    key = "requestBody" if section == "request_body" else "responseBody"
    text = record[key]
    end = offset + limit
    return dict(id=request_id, section=section, content=text[offset:end], note=record[key + "Note"],
                totalCharacters=len(text), nextOffset=end if end < len(text) else None)
