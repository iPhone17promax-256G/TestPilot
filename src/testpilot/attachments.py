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

# 功能：筛选附件可公开的元信息，避免把内部磁盘路径等字段直接交给界面或模型。
# 输入：record 为附件 payload，必须含 ID、名称、体积、类型、正文统计、预览和下载地址等字段。
# 输出/影响：返回仅含白名单字段的新字典；不读取正文、不改变原记录，不写文件或数据库。
def public_attachment(record):
    return {k: record[k] for k in ("id", "name", "size", "type", "characters", "pages", "warnings", "preview", "createdAt", "downloadUrl")}

# 先校验文件，再在有超时的独立 Python 子进程解析；解析失败只清理本次新建文件。
# 子进程与超时/资源限制降低解析拖垮 API 的影响，不是完整操作系统沙箱。
# 功能：校验并保存对话附件，在独立子进程提取正文后登记元信息。
# 输入：name 为原文件名，content 为上传字节；文档格式和最大 10 MB 由 documents 规则校验。
# 输出/影响：返回公开附件字典；原文件与 text.txt 写数据目录 attachments/ATT-ID，records(kind=attachment) 存路径/摘要/元信息；失败清理本次文件，解析超时 30 秒。
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
            output = subprocess.run([sys.executable, "-m", "testpilot.documents", str(original)],
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
        # 上传或文档解析失败时，只清理这次上传新建的原文件/正文文件，不删除同目录中其他已经成功保存的附件。
        for file in (original, root / "text.txt"):
            file.unlink(missing_ok=True)
        root.rmdir()
        raise

# 功能：把一条消息的上传附件 ID 转换成已授权的简短附件引用。
# 输入：identities 为附件 ID 列表，最多五个且不能重复；每个 ID 用 read_record 校验附件类型与当前访问范围。
# 输出/影响：返回公开附件元信息列表；错误抛 ValueError，只读记录，不加载文件正文、不把附件自动关联某 Task。
def attachment_refs(identities):
    if len(identities) > 5 or len(set(identities)) != len(identities):
        raise ValueError("每条消息最多 5 个附件，附件不能重复")
    return [public_attachment(read_record(identity, "attachment")) for identity in identities]

# 除项目归属外，Agent 场景还检查附件确实出现在当前 Task 的消息里，防止跨对话读取。
# 功能：为界面或 Agent 分页读取附件正文，Agent 场景额外核对附件属于当前对话。
# 输入：id 为附件 ID，offset 为零基字符位置，limit 为本页字符数；task_id 非空时须在该 Task 消息附件引用中找到 id。
# 输出/影响：返回 content/nextOffset/字符数/警告等字典；读取附件 text.txt，检查项目归属与可选对话关联，不修改文件或消息。
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
# 功能：将附件目录追加到用户消息文字中，让模型知道有哪些文件可按需读取。
# 输入：message 为保存的消息字典，content 为正文，attachments 为已校验的附件引用列表。
# 输出/影响：返回原文字加简短附件 JSON 的字符串；不自动加载整份需求，不调用模型、不修改原消息或存储。
def message_content(message):
    """Metadata stays small; the model explicitly reads document pages through a tool."""
    text = message.get("content", "")
    attachments = message.get("attachments", [])
    if attachments:
        metadata = [{k: a[k] for k in ("id", "name", "characters", "warnings")} for a in attachments]
        text += "\n本条消息的附件（通过 attachments_read 按 ID 分页读正文）：\n" + json.dumps(metadata, ensure_ascii=False)
    return text
