# 文档文字提取器：PDF 用 pypdf，Word 用 python-docx，纯文本按编码解码。
# 只提取文字/表格，不执行文档宏；没有 OCR，扫描 PDF 和图片里的需求不能假装已读懂。

"""Bounded local document extraction. No document code or macro is executed."""
import io
import json
import sys
import zipfile
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024
MAX_CHARS = 300000
MAX_PAGES = 200
EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv", ".json", ".yaml", ".yml"}

# 功能：检查对话附件的简单文件名及可解析扩展名，防止上传名称带路径或控制字符。
# 输入：name 是用户上传的原始文件名，支持 PDF/DOCX 和常见纯文本格式，不支持旧 DOC。
# 输出/影响：返回原文件名；不合法抛 ValueError，不写文件、不解析文档正文。
def validate_name(name):
    if not name or len(name) > 180 or "/" in name or "\\" in name or any(ord(c) < 32 for c in name):
        raise ValueError("文件名不合法")
    if Path(name).suffix.lower() not in EXTENSIONS:
        raise ValueError("支持 PDF、DOCX、TXT、Markdown、CSV、JSON、YAML；旧版 DOC 请先另存为 DOCX")
    return name

# 统一输出 text、字符数、页数和 warnings；限制原文件、解压体积、页数及正文长度。
# 提取结果只是原文的可读表示，不包含模型理解、知识库向量检索或自动生成测试。
# 功能：在资源限制下从文档提取文字及表格，供上传流程生成可分页读取的正文。
# 输入：path 为已经保存到本机的原文件路径；按扩展名选择 pypdf/python-docx 或文本解码，同时检查大小和内容上限。
# 输出/影响：返回 text/characters/pages/warnings 字典；读取原文件，只在内存提取文本，不执行宏、不 OCR、不调用模型或写数据库。
def extract_document(path):
    path = Path(path)
    extension = path.suffix.lower()
    if extension not in EXTENSIONS:
        raise ValueError("不支持的文件格式")
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("文件不能为空，单个文件最多 10 MB")
    pieces, warnings, total = [], [], 0
    # 功能：积累一段非空正文并检查整份文档提取后的字符上限。
    # 输入：value 为当前页、段落、表格行或文本文件内容；pieces 和 total 由 extract_document 闭包提供，total 用 nonlocal 更新。
    # 输出/影响：无返回值；修改内存 pieces/total，超出上限抛 ValueError，不向磁盘或数据库写入内容。
    def add(value):
        nonlocal total
        total += len(value) + 2
        if total > MAX_CHARS:
            raise ValueError("文档正文超过 30 万字，请拆分后上传")
        if value.strip(): pieces.append(value.strip())
    pages = 0
    if extension == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("PDF 已加密，请上传解除密码保护的副本")
        pages = len(reader.pages)
        if pages > MAX_PAGES:
            raise ValueError("PDF 最多 200 页，请拆分后上传")
        empty = []
        for index, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if text.strip(): add(f"## 第 {index} 页\n{text}")
            else: empty.append(str(index))
        if not pieces:
            raise ValueError("PDF 没有可提取的文字，可能是扫描件；请先 OCR 或上传文字版")
        if empty:
            warnings.append("第 " + "、".join(empty[:20]) + " 页未提取到文字，图片内容未识别，请检查原文")
    elif extension == ".docx":
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 1500 or sum(e.file_size for e in entries) > 40 * 1024 * 1024:
                raise ValueError("Word 文档解压内容过大，请拆分后上传")
            if "word/document.xml" not in archive.namelist():
                raise ValueError("不是有效的 DOCX 文档")
        from docx import Document
        from docx.table import Table
        document = Document(path)
        for block in document.iter_inner_content():
            if isinstance(block, Table):
                for row in block.rows:
                    add(" | ".join(cell.text.replace("\n", " / ") for cell in row.cells))
            else:
                add(block.text)
        if document.inline_shapes:
            warnings.append("Word 中的图片未做 OCR，正文和表格已提取，请检查图片中的需求")
    else:
        raw = path.read_bytes()
        encodings = ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
        for encoding in encodings:
            try:
                text = raw.decode(encoding)
                if "\x00" in text: continue
                add(text)
                break
            except UnicodeError:
                continue
        else:
            raise ValueError("文本编码无法识别，请保存为 UTF-8 后上传")
    content = "\n\n".join(pieces)
    if not content:
        raise ValueError("文档没有可读取的正文")
    return {"text": content, "characters": len(content), "pages": pages, "warnings": warnings}

# 供上传流程启动的子进程入口，Linux 下加内存/CPU 上限，并用 JSON 回传解析结果。
# 功能：提供独立 Python 文档解析子进程入口，设置 Linux 资源上限并回传结构化结果。
# 输入：无函数参数；sys.argv[1] 是调用方上传流程提供的原文件路径。
# 输出/影响：无返回值；向标准输出打印成功或失败 JSON，Linux 限制内存/CPU；异常隐藏内部堆栈，正文保存由父进程完成。
def main():
    try:
        if sys.platform == "linux":
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
        result = extract_document(sys.argv[1])
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    except ValueError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
    except Exception:
        print(json.dumps({"ok": False, "error": "文档解析失败，请检查文件是否损坏或格式是否正确"}, ensure_ascii=False))

if __name__ == "__main__":
    main()
