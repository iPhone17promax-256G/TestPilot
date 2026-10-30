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


# 功能：检查项目资料上传名称是单一合法文件名，防止带目录和控制字符。
# 输入：name 为用户提供的原始资料名；这里不限制解析扩展名，因为非文档文件也可保存原件。
# 输出/影响：通过时无返回值，不合法抛 ValueError；不创建文件、不解析正文。
def validate_filename(name):
    if not name.strip() or len(name) > 180 or name in {".", ".."} or any(c in name for c in "/\\") or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("文件名不合法")


# 功能：根据规范 SRC ID 定位资料目录，并拒绝目录符号链接改变实际存储位置。
# 输入：identity 为形如 SRC-加十二位大写十六进制的资料 ID，数据根目录来自 settings.data。
# 输出/影响：返回数据目录 sources/ID 的 Path；无效 ID 或解析后路径变化抛 ValueError，本函数不创建目录或查询访问权限。
def file_root(identity):
    if not re.fullmatch(r"SRC-[0-9A-F]{12}", identity):
        raise ValueError("资料文件编号不合法")
    base = settings.data.resolve()
    root = base / "sources" / identity
    if root.resolve() != root or root.parent.resolve() != root.parent:
        raise ValueError("资料文件路径不合法")
    return root


# 功能：在已校验资料目录中取得原件或解析正文的实际文件路径。
# 输入：record 为资料记录，提供 id/extension；text 为 True 选择 text.txt，否则选择 original 加扩展名。
# 输出/影响：返回存在的普通文件 Path；名称异常、符号链接或文件缺失抛 ValueError，不读取正文、不在这里查询项目归属。
def file_path(record, text=False):
    root = file_root(record["id"])
    name = "text.txt" if text else "original" + record["extension"]
    if Path(name).name != name:
        raise ValueError("资料文件路径不合法")
    path = root / name
    if path.resolve() != path or not path.is_file():
        raise ValueError("找不到资料文件")
    return path


# 功能：把文字资料和文件资料统一成界面与 Agent 使用的公开索引。
# 输入：record 为 source payload；缺少旧字段时由 content 或当前项目上下文计算兼容默认值。
# 输出/影响：返回 ID/名称/体积/可读状态/预览/警告/下载链接等字典；不公开存储路径、不读取全文、不修改记录。
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


# 功能：取得当前账号和项目可访问的全部资料业务记录。
# 输入：无显式参数；访问范围由 storage.list_records 使用当前请求或执行上下文控制。
# 输出/影响：返回 source payload 列表，包含归档项；只查询数据库，不加载文件正文。
def source_records():
    from .storage import list_records
    return list_records("source")


# 功能：把当前项目的全部资料记录转换为统一公开元信息列表。
# 输入：无显式参数；source_records 负责获取已授权记录，public_source 负责字段筛选。
# 输出/影响：返回公开资料字典列表，含归档状态供界面过滤；不解析文档、不读取原件或写存储。
def sources_list():
    return [public_source(row) for row in source_records()]


# 先锁项目记录，再检查文件数/总容量并新增，避免同时上传绕过总配额。
# 归档只是隐藏，不删除磁盘数据，所以仍计算占用。
# 功能：在事务内串行核对项目资料配额并登记新增资料，避免并发上传超额。
# 输入：payload 是已准备好的资料元信息或文字内容，必须有 id/size；当前项目由 ContextVar 提供。
# 输出/影响：返回公开资料字典；锁定项目记录，计入归档资料体积后写 records(kind=source)，超过 200 份或 200 MB 抛错；文件写入由调用者负责。
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


# 功能：把手动输入的项目说明作为文字资料保存。
# 输入：name 为资料显示名称，content 为非空文字正文；当前账号/项目由调用上下文提供。
# 输出/影响：返回公开资料索引；生成 SRC ID，通过 add_source 写正文到数据库 payload；此路线不创建原件文件或 text.txt。
def save_source(name, content):
    from .testing import stamp
    if not name.strip() or not content.strip():
        raise ValueError("资料名称和正文不能为空")
    from .knowledge_vectors import digest
    return add_source(dict(id=uid("SRC"), name=name.strip(), content=content, textHash=digest(content), characters=len(content), size=len(content.encode("utf-8")), createdAt=stamp()))


# 功能：启动有超时的文档解析子进程，并把常见执行或解码失败转换为可展示结果。
# 输入：path 为已写入本机资料目录的原文件路径，子进程用当前 Python 运行 testpilot.documents。
# 输出/影响：返回带 ok 的解析结果字典或友好 error；最多等待 30 秒，只读取/解析文件，不保存 source 记录或正文文件。
def parse_file(path):
    try:
        output = subprocess.run([sys.executable, "-m", "testpilot.documents", str(path)], capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        if output.returncode:
            return dict(ok=False, error="文档解析超过资源限制，请拆分文档或上传文字版")
        return json.loads(output.stdout)
    except subprocess.TimeoutExpired:
        return dict(ok=False, error="文档解析超时，请缩小文档或上传文字版")
    except (ValueError, OSError):
        return dict(ok=False, error="文档解析失败，请下载原文件核对或上传文字版")


# 原文件可保存为资料；支持的文档格式额外解析文字，不支持的格式明确标记不可读。
# 功能：保存项目资料原件；可解析格式另外提取文字，其他格式保留原件并标记不可读。
# 输入：name 为合法原文件名，content 为不超过 10 MB 的非空上传字节；扩展名决定是否调用 parse_file。
# 输出/影响：返回公开资料索引；文件写 sources/SRC-ID，元信息经 add_source 入数据库；解析失败可保存不可读资料，后续登记异常则清理本次目录。
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
        if readable:
            from .knowledge_vectors import digest
            payload["textHash"]=digest(parsed["text"])
        return add_source(payload)
    except Exception:
        for path in (original, root / "text.txt"):
            path.unlink(missing_ok=True)
        root.rmdir()
        raise


# 功能：根据资料存储类型读取完整文字，统一文字资料与解析后的文件资料。
# 输入：record 为已由上层校验访问权限的资料 payload；storage=file 读取 text.txt，否则取 content。
# 输出/影响：返回正文字符串；可能读磁盘，不自动做分页、不调用模型、不修改文件或数据库。
def source_text(record):
    if record.get("storage")=="file" and settings.role=="executor" and settings.executor_claim:
        path=file_root(record["id"])/"text.txt"
        if not path.exists():
            from .replica import download
            download(path.relative_to(settings.data).as_posix())
    return file_path(record, text=True).read_text(encoding="utf-8") if record.get("storage") == "file" else record["content"]


# 按字符范围返回正文与 nextOffset；模型想看更多需主动再读，不一次填满上下文。
# 功能：按字符范围读取未归档项目资料，供 Agent 按需理解需求而不一次占满上下文。
# 输入：id 为 source ID，offset 为零基字符位置，limit 为 1～16000 字符；记录归属由 read_record 检查。
# 输出/影响：返回本页 content、nextOffset、可读状态和警告等；不可读文件返回原件下载提示，归档资料拒绝读取；仅查询和读文件。
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
# 功能：按名称与可读正文进行大小写无关的关键词匹配，并分页返回资料摘要。
# 输入：query 为搜索文字，offset 为匹配项零基起点，limit 为返回条数；检索当前项目未归档资料。
# 输出/影响：返回匹配索引字典列表；必要时读正文但只返回预览，不做 embedding 或模型语义检索，不修改记录。
def keyword_search_sources(query, offset=0, limit=12):
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
    # 搜索仍返回资料列表，保持已有调用格式；offset 控制返回哪一页匹配项，需要更多结果的调用者可增大 offset 再请求，不是一次读完所有正文。
    return matches[offset:end]


def search_sources(query, offset=0, limit=12, mode="auto"):
    """Agent 复用资料搜索：已手动建库时检索片段，否则继续关键词；不建立索引。"""
    if settings.role=="executor" and settings.executor_claim:
        from .replica import request
        return request("POST","/api/knowledge/search",json=dict(query=query,offset=offset,limit=limit,mode=mode)).json()["items"]
    from .knowledge import search
    return search(query,offset,limit,mode)["items"]


# 功能：为 Agent 提供当前项目的简短资料目录及总量提示。
# 输入：无显式参数；由 sources_list 获取公开元信息，过滤 archived 项并截取最多 50 份资料。
# 输出/影响：返回 sources/sourceCount/moreSources 字典；每份摘要最多 160 字，只读索引，不把全部正文送入提示词。
def source_catalog():
    active = [s for s in sources_list() if not s["archived"]]
    return dict(sources=[dict(id=s["id"], name=s["name"], readable=s["readable"], type=s["type"], summary=s["preview"][:160]) for s in active[:50]],
                sourceCount=len(active), moreSources=len(active) > 50)


# 功能：切换资料的归档状态，以可恢复方式从活跃目录隐藏资料。
# 输入：id 为当前项目 source ID，archived 为是否归档的布尔值，先由 read_record 校验归属。
# 输出/影响：返回公开资料字典；update_record 更新 payload 的 archived/status，保留数据库正文和原件文件，归档不会释放存储配额。
def archive_source(id, archived):
    read_record(id, "source")
    record = update_record(id, lambda p: p.update(archived=archived, status="archived" if archived else "ready"))
    return public_source(record)
