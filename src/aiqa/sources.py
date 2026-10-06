# 项目资料库：同项目多段对话共用的文档/文件，与某条聊天的附件不同。
# 支持原文件保存、文字提取、关键词搜索和归档；当前搜索不是 embedding 向量检索。

"""Project materials: immutable local files, bounded text reads, and reversible archive."""
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote
from sqlalchemy import select
from .config import settings
from .documents import EXTENSIONS, MAX_BYTES
from .storage import Record, read_record, session, uid, update_record
from .workspace import current_project

MAX_FILES = 200
MAX_PROJECT_BYTES = 200 * 1024 * 1024


def validate_filename(name):
    if not name.strip() or len(name) > 180 or name in {".", ".."} or any(c in name for c in "/\\") or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("文件名不合法")


def file_root(identity):
    if not re.fullmatch(r"SRC-[0-9A-F]{12}", identity):
        raise ValueError("资料文件编号不合法")
    base = settings.data.resolve()
    root = base / "sources" / identity
    if root.resolve() != root or root.parent.resolve() != root.parent:
        raise ValueError("资料文件路径不合法")
    return root


def file_path(record, text=False):
    root = file_root(record["id"])
    name = "text.txt" if text else "original" + record["extension"]
    if Path(name).name != name:
        raise ValueError("资料文件路径不合法")
    path = root / name
    if path.resolve() != path or not path.is_file():
        raise ValueError("找不到资料文件")
    return path


def public_source(record):
    file = record.get("storage") == "file"
    return dict(id=record["id"], projectId=record.get("projectId", current_project()), name=record["name"],
                storage="file" if file else "text", type=record.get("type", "text"),
                size=record.get("size", len(record.get("content", "").encode("utf-8"))),
                characters=record.get("characters", len(record.get("content", ""))),
                pages=record.get("pages", 0), warnings=record.get("warnings", []),
                readable=record.get("readable", True), preview=record.get("preview", record.get("content", "")[:300]),
                createdAt=record.get("createdAt", ""), archived=record.get("archived", False),
                downloadUrl=f"/api/sources/{record['id']}/download?project_id={quote(record.get('projectId', current_project()))}")


def source_records():
    from .storage import list_records
    return list_records("source")


def sources_list():
    return [public_source(row) for row in source_records()]


# 先锁项目记录，再检查文件数/总容量并新增，避免同时上传绕过总配额。
# 归档只是隐藏，不删除磁盘数据，所以仍计算占用。
def add_source(payload):
    """Serialize quota checks across concurrent uploads, including archived files."""
    with session() as db:
        project = db.scalar(select(Record).where(Record.id == current_project(), Record.kind == "project").with_for_update())
        if not project:
            raise ValueError("项目不存在")
        rows = list(db.scalars(select(Record).where(Record.kind == "source", Record.project_id == current_project())))
        used = sum(row.payload.get("size", len(row.payload.get("content", "").encode("utf-8"))) for row in rows)
        if len(rows) >= MAX_FILES or used + payload["size"] > MAX_PROJECT_BYTES:
            raise ValueError("项目资料最多 200 份、总计 200 MB；归档资料仍占用存储")
        db.add(Record(id=payload["id"], kind="source", status="ready", payload=payload))
    return public_source(dict(payload, projectId=current_project()))


def save_source(name, content):
    from .testing import stamp
    if not name.strip() or not content.strip():
        raise ValueError("资料名称和正文不能为空")
    return add_source(dict(id=uid("SRC"), name=name.strip(), content=content, size=len(content.encode("utf-8")), createdAt=stamp()))


def parse_file(path):
    try:
        output = subprocess.run([sys.executable, "-m", "aiqa.documents", str(path)], capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        if output.returncode:
            return dict(ok=False, error="文档解析超过资源限制，请拆分文档或上传文字版")
        return json.loads(output.stdout)
    except subprocess.TimeoutExpired:
        return dict(ok=False, error="文档解析超时，请缩小文档或上传文字版")
    except (ValueError, OSError):
        return dict(ok=False, error="文档解析失败，请下载原文件核对或上传文字版")


# 原文件可保存为资料；支持的文档格式额外解析文字，不支持的格式明确标记不可读。
def upload_source(name, content):
    from .testing import stamp
    validate_filename(name)
    if not 0 < len(content) <= MAX_BYTES:
        raise ValueError("文件不能为空，单个文件最多 10 MB")
    identity = uid("SRC")
    extension = Path(name).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,16}", extension):
        extension = ".bin"
    root = file_root(identity)
    root.mkdir(parents=True, exist_ok=False)
    original = root / ("original" + extension)
    try:
        original.write_bytes(content)
        parsed = parse_file(original) if extension in EXTENSIONS else dict(ok=False, error="此格式只保存原文件，暂不提供正文解析；请另传文字说明供 Agent 使用")
        readable = bool(parsed.get("ok"))
        if readable:
            (root / "text.txt").write_text(parsed["text"], encoding="utf-8")
        payload = dict(id=identity, name=name, storage="file", extension=extension, type=extension[1:],
                       size=len(content), readable=readable, characters=parsed.get("characters", 0),
                       pages=parsed.get("pages", 0), warnings=parsed.get("warnings", []) if readable else [parsed["error"]],
                       preview=parsed.get("text", "")[:300], createdAt=stamp())
        return add_source(payload)
    except Exception:
        for path in (original, root / "text.txt"):
            path.unlink(missing_ok=True)
        root.rmdir()
        raise


def source_text(record):
    return file_path(record, text=True).read_text(encoding="utf-8") if record.get("storage") == "file" else record["content"]


# 按字符范围返回正文与 nextOffset；模型想看更多需主动再读，不一次填满上下文。
def read_source(id, offset=0, limit=8000):
    if offset < 0 or not 1 <= limit <= 16000:
        raise ValueError("正文读取范围不合法")
    record = read_record(id, "source")
    if record.get("archived"):
        raise ValueError("资料已归档，请先恢复后再读取")
    metadata = public_source(record)
    if not metadata["readable"]:
        return dict(id=id, name=record["name"], readable=False, content="", offset=offset, nextOffset=None,
                    characters=0, warnings=metadata["warnings"], downloadUrl=metadata["downloadUrl"])
    text = source_text(record)
    end = offset + limit
    return dict(id=id, name=record["name"], readable=True, content=text[offset:end], offset=offset,
                nextOffset=end if len(text) > end else None, characters=len(text), warnings=metadata["warnings"])


# 按名称和已解析正文做大小写无关的字符串匹配；规模小易理解，但不等同于语义搜索。
def search_sources(query, offset=0, limit=12):
    matches = []
    needle = query.strip().casefold()
    for record in source_records():
        if record.get("archived"):
            continue
        text = ""
        if needle not in record["name"].casefold() and record.get("readable", True):
            text = source_text(record)
        if needle in (record["name"] + text).casefold():
            item = public_source(record)
            matches.append({k: item[k] for k in ("id", "name", "readable", "type", "characters", "warnings")} | {"summary": item["preview"]})
    end = offset + limit
    # Keep the existing list result compatible; callers can request another offset.
    return matches[offset:end]


def source_catalog():
    active = [s for s in sources_list() if not s["archived"]]
    return dict(sources=[dict(id=s["id"], name=s["name"], readable=s["readable"], type=s["type"], summary=s["preview"][:160]) for s in active[:50]],
                sourceCount=len(active), moreSources=len(active) > 50)


def archive_source(id, archived):
    read_record(id, "source")
    record = update_record(id, lambda p: p.update(archived=archived, status="archived" if archived else "ready"))
    return public_source(record)
