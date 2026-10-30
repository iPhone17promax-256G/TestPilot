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
# 功能：按最新有效用户消息确定当前轮的网络记录策略，防止旧轮次设置误用到新轮。
# 输入：task_id 为 Agent 任务 ID；messages 中用户消息的 captureLevel 提供初始级别，默认 api。
# 输出/影响：返回 level/locked/turnId/reason 字典；新轮次时更新 Task.networkCapture，用户 off 会锁定本轮。
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
# 功能：让 Agent 调整当前轮后续浏览器操作的采集级别，但保留用户关闭的约束。
# 输入：task_id 为当前任务；level 为 off/api/detailed；reason 为模型给出的调整理由。
# 输出/影响：返回策略及生效范围说明；写 Task.networkCapture，不修改已结束证据、不补采旧正文，锁定 off 不能重开。
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


# 功能：合并任务轮次策略和单次运行请求的级别，统一应用用户的关闭限制。
# 输入：task_id 可为空；requested 为可选单次采集级别，未指定则使用任务策略或默认 api。
# 输出/影响：返回校验后的级别字符串；有任务时可能初始化轮次策略，锁定用户 off 时始终返回 off。
def resolve_level(task_id="", requested=None):
    policy = task_policy(task_id) if task_id else {"level": "api", "locked": False}
    if policy["locked"]:
        return "off"
    return validate_level(requested or policy["level"])


# 功能：为一个 Web Run 或页面探索创建独立的网络证据归属记录。
# 输入：owner_id 为 Run/inspection ID；kind 为 run/inspection；relative 为证据相对目录；target_url/level 为范围与级别；task_id 为可选关联任务。
# 输出/影响：返回 network_session 字典并写业务记录；仅登记元信息，实际请求文件由 NetworkRecorder 产生。
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
# 功能：读取网络会话并检查关联 Task 及所属运行记录可访问性。
# 输入：identity 为 NET 会话 ID；task_id 为可选当前对话限定，read_record 同时应用账号/项目访问检查。
# 输出/影响：返回内部网络会话字典；不写记录、不读正文，归属不符或资源不可访问时报错。
def owned_session(identity, task_id=""):
    capture = read_record(identity, "network_session")
    if task_id and capture.get("taskId") != task_id:
        raise ValueError("当前会话中找不到该网络记录")
    owner = read_record(capture["ownerId"] if capture["kind"] == "run" else capture["taskId"],
                        "run" if capture["kind"] == "run" else "task")
    return capture


# 功能：在已授权网络会话目录中，限大小读取一份 JSON 证据，拒绝链接逃逸。
# 输入：capture 为 owned_session 返回的会话；filename 为固定索引或请求文件名；maximum 为字节上限。
# 输出/影响：返回解析的数据，文件不存在返回 None；只读目录内普通文件，越界、超限或解析错误会报错。
def _json_file(capture, filename, maximum):
    # 读取前已检查网络记录的账号/项目/对话归属；随后仍对详情文件 resolve 并检查目录边界，不能让符号链接把读取导向该记录目录之外。
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


# 功能：读取索引形成会话摘要，必要时把结束状态写回业务记录。
# 输入：identity 为会话 ID；task_id 为可选对话限定；finish 表示执行已收尾，应持久化最终摘要。
# 输出/影响：返回不含内部 path 的摘要；只包含计数/状态/限额等，finish 时更新记录，不把请求头或正文自动给模型。
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


# 功能：按页列出当前可访问网络会话，或指定会话里符合查询条件的请求概要。
# 输入：session_id 为空列会话；query 为方法/URL 文本；errors_only 筛失败；offset/limit 为分页；task_id 限定对话。
# 输出/影响：返回列表、total、nextOffset 和可选会话摘要；只读记录/索引，概要不含完整正文。
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
# 功能：按请求编号读取元数据或一段正文，供 Agent 按需调查网络证据。
# 输入：session_id 为 NET ID；request_id 为 REQ-四位数；section 选 metadata/request_body/response_body；offset/limit 按字符分页；task_id 限定对话。
# 输出/影响：返回元数据或正文片段及后续位置；只读已保存文件，检查归属/大小，采集未保存的正文无法补回。
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
