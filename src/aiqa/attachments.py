# 对话附件：原文件和提取文本保存在数据目录，数据库保存路径及有限元信息。
# 模型先看到文件 ID/名称/警告，再通过工具分页读取正文，避免把整份需求塞进每次提示词。

"""Immutable chat documents with project and conversation access checks."""
import hashlib
import json
import subprocess
import sys
from urllib.parse import quote
from .config import settings
from .documents import MAX_BYTES, validate_name
from .storage import Record, session, uid, read_record
from .workspace import current_project

def public_attachment(record):
    return {k: record[k] for k in ("id", "name", "size", "type", "characters", "pages", "warnings", "preview", "createdAt", "downloadUrl")}

# 先校验文件，再在有超时的独立 Python 子进程解析；解析失败只清理本次新建文件。
# 子进程与超时/资源限制降低解析拖垮 API 的影响，不是完整操作系统沙箱。
def upload_attachment(name, content):
    validate_name(name)
    if not 0 < len(content) <= MAX_BYTES:
        raise ValueError("文件不能为空，单个文件最多 10 MB")
    from .testing import safe_path, stamp
    from pathlib import Path
    identity = uid("ATT")
    relative = f"attachments/{identity}"
    root = safe_path(relative)
    root.mkdir(parents=True, exist_ok=False)
    original = root / ("original" + Path(name).suffix.lower())
    original.write_bytes(content)
    try:
        try:
            output = subprocess.run([sys.executable, "-m", "aiqa.documents", str(original)],
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
        except subprocess.TimeoutExpired:
            raise ValueError("文档解析超时，请缩小文档或上传文字版") from None
        if output.returncode:
            raise ValueError("文档解析超过资源限制，请拆分文档或上传文字版")
        parsed = json.loads(output.stdout)
        if not parsed.get("ok"):
            raise ValueError(parsed.get("error", "文档解析失败"))
        (root / "text.txt").write_text(parsed["text"], encoding="utf-8")
        payload = dict(id=identity, name=name, size=len(content), type=Path(name).suffix.lower()[1:],
                       characters=parsed["characters"], pages=parsed["pages"], warnings=parsed["warnings"],
                       preview=parsed["text"][:300], hash=hashlib.sha256(content).hexdigest(), createdAt=stamp(),
                       path=f"{relative}/{original.name}", textPath=f"{relative}/text.txt",
                       downloadUrl=f"/api/attachments/{identity}/download?project_id={quote(current_project())}")
        with session() as db:
            db.add(Record(id=identity, kind="attachment", status="ready", payload=payload))
        return public_attachment(payload)
    except Exception:
        # Only files created by this upload attempt are removed.
        for file in (original, root / "text.txt"):
            file.unlink(missing_ok=True)
        root.rmdir()
        raise

def attachment_refs(identities):
    if len(identities) > 5 or len(set(identities)) != len(identities):
        raise ValueError("每条消息最多 5 个附件，附件不能重复")
    return [public_attachment(read_record(identity, "attachment")) for identity in identities]

# 除项目归属外，Agent 场景还检查附件确实出现在当前 Task 的消息里，防止跨对话读取。
def read_attachment(id, offset=0, limit=8000, task_id=""):
    if offset < 0 or not 1 <= limit <= 16000:
        raise ValueError("正文读取范围不合法")
    record = read_record(id, "attachment")
    if task_id:
        task = read_record(task_id, "task")
        attached = {a["id"] for m in task.get("messages", []) for a in m.get("attachments", [])}
        if id not in attached:
            raise ValueError("该附件未关联当前对话")
    from .testing import safe_path
    text = safe_path(record["textPath"]).read_text(encoding="utf-8")
    end = offset + limit
    return dict(id=id, name=record["name"], content=text[offset:end], offset=offset,
                nextOffset=end if len(text) > end else None, characters=len(text), warnings=record["warnings"])

# 把附件索引补进用户消息；正文不会自动全部发送给模型，模型需调用 attachments_read。
def message_content(message):
    """Metadata stays small; the model explicitly reads document pages through a tool."""
    text = message.get("content", "")
    attachments = message.get("attachments", [])
    if attachments:
        metadata = [{k: a[k] for k in ("id", "name", "characters", "warnings")} for a in attachments]
        text += "\n本条消息的附件（通过 attachments_read 按 ID 分页读正文）：\n" + json.dumps(metadata, ensure_ascii=False)
    return text
