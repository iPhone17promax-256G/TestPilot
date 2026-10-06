# 项目扩展配置：Skill 是按需读取的说明书，MCP 是连接外部工具的协议配置。
# 启用 Skill 不会自动执行脚本；模型读取说明后仍需通过具体 Tool 发起动作。

"""Project-scoped instructions and external tool connections."""
import hashlib
import io
import json
import os
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
import yaml
from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from .config import settings
from .storage import Record, Job, session, list_records, read_record, update_record, uid, assert_project
from .workspace import current_project

BUILTINS = {
    "api-testing": "HTTPX 接口测试：鉴权、参数边界、业务断言与数据清理。",
    "web-testing": "观察页面、稳定定位、Playwright 自动等待与 UI 断言。",
    "performance-testing": "JMeter 计划、低负载联调、压测指标与阈值。",
    "app-testing": "Android / Appium：设备检查、页面观察与回归验证。",
    "failure-analysis": "根据报告、日志和截图分析测试失败。",
    "aiqa-guide": "AIQA 用例版本、运行、报告与工具使用方法。",
}
SKILL_ROOT = Path("/app/skills")
MAX_SERVERS = 8

def stamp():
    from .testing import stamp as format_stamp
    return format_stamp()

def resource_name(name):
    path = PurePosixPath(name)
    if not name or len(name)>180 or "\\" in name or "\x00" in name or path.is_absolute() or any(p in {"..",".",""} for p in name.split("/")):
        raise ValueError("资源路径必须是 Skill 内的相对文件名")
    return str(path)

class SkillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = ""
    content: str = Field(min_length=1, max_length=150000)
    resources: dict[str,str] = Field(default_factory=dict, max_length=50)
    enabled: bool = True

# 解析 SKILL.md 的 YAML 头，提取 name/description 作为目录；正文稍后按需读取。
def parse_skill(content):
    if not content.startswith("---\n") and not content.startswith("---\r\n"):
        raise ValueError("SKILL.md 需要 YAML 开头，包含 name 和 description")
    parts = re.split(r"^---\s*$", content, maxsplit=2, flags=re.MULTILINE)
    if len(parts)!=3 or len(parts[1])>4000:
        raise ValueError("Skill 元信息格式不正确或过长")
    # Reject aliases rather than permitting recursive YAML structures.
    try:
        if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(parts[1])):
            raise ValueError("Skill 元信息不能使用 YAML 别名")
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        raise ValueError("Skill 元信息不是有效 YAML") from None
    if not isinstance(meta,dict) or not isinstance(meta.get("name"),str) or not isinstance(meta.get("description"),str):
        raise ValueError("Skill 需要文本 name 和 description")
    name, description = meta["name"], meta["description"].strip()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*",name) or len(name)>64:
        raise ValueError("Skill 名称使用小写英文、数字和短横线，最多 64 字符")
    if name in BUILTINS:
        raise ValueError("自定义 Skill 不能覆盖内置 Skill")
    if not description or len(description)>1000 or not parts[2].strip():
        raise ValueError("请填写 Skill 描述和正文，描述最多 1000 字")
    return name, description

def validate_skill(body):
    if len(body.content.encode())>150000:raise ValueError("SKILL.md 最多 150 KB")
    name, description = parse_skill(body.content)
    files = {"SKILL.md":body.content}
    for path, text in body.resources.items():
        path = resource_name(path)
        if path=="SKILL.md":raise ValueError("正文请填写到 content 字段")
        if not isinstance(text,str) or len(text.encode())>150000 or "\x00" in text:
            raise ValueError("辅助资源只支持 UTF-8 文本，每个最多 150 KB")
        files[path] = text
    if sum(len(text.encode()) for text in files.values())>1_000_000:
        raise ValueError("一个 Skill 的解压内容最多 1 MB")
    return name, description, files

def skill_public(payload, detail=False):
    result={k:payload[k] for k in ["id","name","description","enabled","revision","updatedAt"] if k in payload}
    result.update(builtin=False,resources=sorted(payload["files"]))
    if detail:result.update(content=payload["files"]["SKILL.md"],files=payload["files"])
    return result

def skills_catalog(enabled_only=True):
    result=[dict(id=name,name=name,description=description,enabled=True,builtin=True,revision=1,resources=["SKILL.md"]) for name,description in BUILTINS.items()]
    result += [skill_public(row) for row in list_records("skill") if not enabled_only or row["enabled"]]
    return result

def skill_save(body):
    name, description, files = validate_skill(body)
    with session() as db:
        # Lock the project to serialize names and catalog limits.
        db.scalar(select(Record).where(Record.id==current_project()).with_for_update())
        existing=list(db.scalars(select(Record).where(Record.kind=="skill",Record.project_id==current_project())))
        if any(r.id!=body.id and r.payload["name"]==name for r in existing):
            raise ValueError("当前项目已存在同名 Skill")
        if not body.id and len(existing)>=30:raise ValueError("每个项目最多 30 个自定义 Skill")
        row=db.get(Record,body.id) if body.id else None
        if body.id:
            if not row or row.kind!="skill":raise ValueError("Skill 不存在")
            assert_project(row)
        identity=body.id or uid("SKILL")
        payload=dict(id=identity,name=name,description=description,enabled=body.enabled,files=files,
                     revision=(row.payload["revision"]+1 if row else 1),updatedAt=stamp(),projectId=current_project())
        if row:row.payload=payload
        else:db.add(Record(id=identity,kind="skill",payload=payload))
    return skill_public(payload,True)

# 导入 Markdown 或 ZIP，限制解压大小、文件数量和相对路径，拒绝链接及重复文件名。
# 导入仅保存文字资源，绝不执行包里的脚本。
def skill_import(filename, data):
    if len(data)>5_000_000:raise ValueError("Skill 上传文件最多 5 MB")
    if filename.lower().endswith(".md"):
        try:content=data.decode("utf-8-sig")
        except UnicodeError:raise ValueError("SKILL.md 请保存为 UTF-8") from None
        return skill_save(SkillInput(content=content))
    if not filename.lower().endswith(".zip"):raise ValueError("请选择 SKILL.md 或 Skill ZIP")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries=[item for item in archive.infolist() if not item.is_dir()]
            if not entries or len(entries)>51 or sum(i.file_size for i in entries)>1_000_000:
                raise ValueError("Skill ZIP 最多 51 个文件、解压后最多 1 MB")
            files={}
            for item in entries:
                resource_name(item.filename)
                if stat.S_ISLNK(item.external_attr>>16) or item.file_size>150000 or item.flag_bits&1:
                    raise ValueError("Skill ZIP 不支持链接、加密或超过 150 KB 的单个文件")
                if item.filename in files:raise ValueError("Skill ZIP 包含重复文件名")
                files[item.filename]=archive.read(item).decode("utf-8-sig")
            if "SKILL.md" not in files:
                roots={p.split("/")[0] for p in files}
                if len(roots)==1:
                    prefix=next(iter(roots))+"/"
                    files={p.removeprefix(prefix):v for p,v in files.items()}
            if "SKILL.md" not in files:raise ValueError("Skill ZIP 根目录需要 SKILL.md")
            content=files.pop("SKILL.md")
            return skill_save(SkillInput(content=content,resources=files))
    except (zipfile.BadZipFile,UnicodeError,RuntimeError):
        raise ValueError("Skill ZIP 无法读取，请检查格式和 UTF-8 编码") from None

# 返回说明或指定资源的一段文本以及后续偏移量。
# 即使资源是 .py，这里也只是读文本；是否复制到工作区并运行由 Agent 决定并经过工具审批。
def skill_read(name, resource="SKILL.md", offset=0, limit=16000):
    resource=resource_name(resource)
    if name in BUILTINS:
        root=(SKILL_ROOT/name).resolve()
        path=(root/resource).resolve()
        if not path.is_relative_to(root) or not path.is_file():raise ValueError("Skill 资源不存在")
        content=path.read_text()
        revision=1
        resources=[str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and not p.is_symlink()]
    else:
        row=next((r for r in list_records("skill") if r["name"]==name),None)
        if not row or not row["enabled"]:raise ValueError("Skill 不存在或未开启")
        if resource not in row["files"]:raise ValueError("Skill 资源不存在")
        content=row["files"][resource];revision=row["revision"];resources=sorted(row["files"])
    end=offset+limit
    return dict(name=name,resource=resource,revision=revision,content=content[offset:end],
                nextOffset=end if end<len(content) else None,totalCharacters=len(content),resources=resources)

def delete_extension(identity, kind):
    with session() as db:
        row=db.get(Record,identity)
        if not row or row.kind!=kind:raise ValueError("扩展不存在")
        assert_project(row)
        db.delete(row)
    return {"status":"deleted"}

# MCP 凭据使用平台本地 Fernet 密钥加密保存；密文与密钥都在部署的信任范围内。
# 这与模型 API Key 仅本地执行器保存的机制不同，不能笼统说“所有密钥都不上平台”。
def cipher():
    path=settings.data/"extension-secrets.key"
    if not path.exists():
        temporary=settings.data/(".extension-key-"+uid("KEY"))
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,"wb") as f:f.write(Fernet.generate_key())
            try:os.link(temporary,path)
            except FileExistsError:pass
        finally:temporary.unlink(missing_ok=True)
    return Fernet(path.read_bytes())

def seal(values):
    return cipher().encrypt(json.dumps(values).encode()).decode()

def credentials(row):
    if settings.role=="executor" and "localCredentials" in row:return row["localCredentials"]
    return json.loads(cipher().decrypt(row["credentials"].encode())) if row.get("credentials") else {"headers":{},"env":{}}

class MCPInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    id: str = ""
    name: str = Field(min_length=1,max_length=80)
    transport: str = Field(default="streamable-http",pattern=r"^(streamable-http|sse|stdio)$")
    url: str = Field(default="",max_length=2000)
    command: str = Field(default="",max_length=300)
    args: list[str] = Field(default_factory=list,max_length=40)
    image: str = Field(default="aiqa-runtime:0.1",min_length=1,max_length=200)
    network: str = Field(default="targets",pattern=r"^(targets|internet|none)$")
    headers: dict[str,str]|None = None
    env: dict[str,str]|None = None
    clear_credentials: bool = False
    enabled: bool = True
    timeout: int = Field(default=30,ge=5,le=120)

    @model_validator(mode="after")
    def validate_connection(self):
        if not self.name.strip():raise ValueError("请填写 MCP 名称")
        if self.transport=="stdio":
            if not self.command or any(c in self.command for c in ["\x00","\n","\r"]):raise ValueError("请填写 stdio 入口命令")
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]*",self.image):raise ValueError("容器镜像名称格式不正确")
        else:
            url=urlsplit(self.url)
            if url.scheme not in {"http","https"} or not url.netloc or url.username or url.password or url.query or url.fragment:
                raise ValueError("MCP 地址需要 http/https；凭据请放请求头，地址不含查询参数")
        for mapping in [self.headers,self.env]:
            if mapping is not None and (len(mapping)>40 or any(not k or len(k)>100 or len(v)>8000 or "\x00" in k+v or "\r" in k+v or "\n" in k+v for k,v in mapping.items())):
                raise ValueError("凭据最多 40 项，键值不能包含换行，单项最多 8000 字")
        if self.headers and any(not re.fullmatch(r"[!#$%&'*+.^_\x60|~0-9A-Za-z-]+",k) for k in self.headers):
            raise ValueError("请求头名称格式不正确")
        if any(len(v)>4000 or "\x00" in v for v in self.args):raise ValueError("命令参数过长或格式不正确")
        return self

def mcp_public(row):
    result={k:v for k,v in row.items() if k not in {"credentials","projectId"}}
    secret=credentials(row)
    result["credentialKeys"]={k:sorted(v) for k,v in secret.items()}
    return result

def mcp_save(body):
    with session() as db:
        db.scalar(select(Record).where(Record.id==current_project()).with_for_update())
        existing=list(db.scalars(select(Record).where(Record.kind=="mcp_config",Record.project_id==current_project())))
        if any(r.id!=body.id and r.payload["name"]==body.name.strip() for r in existing):raise ValueError("当前项目已有同名 MCP")
        if not body.id and len(existing)>=MAX_SERVERS:raise ValueError("每个项目最多 8 个 MCP 连接")
        row=db.get(Record,body.id) if body.id else None
        if body.id:
            if not row or row.kind!="mcp_config":raise ValueError("MCP 配置不存在")
            assert_project(row)
        old=credentials(row.payload) if row and not body.clear_credentials else {"headers":{},"env":{}}
        for field in ["headers","env"]:
            supplied=getattr(body,field)
            if supplied is not None:old[field]=supplied
        payload=body.model_dump(exclude={"headers","env","clear_credentials"})
        payload.update(id=body.id or uid("MCP"),name=body.name.strip(),revision=(row.payload["revision"]+1 if row else 1),
                       credentials=seal(old),updatedAt=stamp(),status="untested",tools=[],error="",projectId=current_project())
        if row:row.payload=payload
        else:db.add(Record(id=payload["id"],kind="mcp_config",payload=payload))
    return mcp_public(payload)

# 把常见 mcpServers 配置转换成平台记录；stdio 命令在隔离容器内运行，不沿用主机 cwd。
def mcp_import(configuration):
    servers=configuration.get("mcpServers")
    if not isinstance(servers,dict) or not servers:raise ValueError("配置需要 mcpServers 对象")
    if len(list_records("mcp_config"))+len(servers)>MAX_SERVERS:raise ValueError("每个项目最多 8 个 MCP 连接")
    bodies=[]
    names={r["name"] for r in list_records("mcp_config")}
    for name,entry in servers.items():
        if not isinstance(entry,dict):raise ValueError("MCP 配置项必须是对象")
        if name in names:raise ValueError("已存在同名 MCP："+name)
        # Never silently discard a host working directory or unsupported command.
        if any(k in entry for k in ["cwd","workingDirectory"]):raise ValueError("stdio 使用隔离容器，不支持主机 cwd；请配置容器镜像内的路径")
        transport=entry.get("transport") or entry.get("type") or ("stdio" if "command" in entry else "streamable-http")
        transport={"http":"streamable-http","streamable_http":"streamable-http"}.get(transport,transport)
        fields={k:entry[k] for k in ["url","command","args","headers","env","image","network","timeout","enabled"] if k in entry}
        bodies.append(MCPInput(name=name,transport=transport,**fields))
    return [mcp_save(body) for body in bodies]

def extension_snapshot():
    return {"skills":skills_catalog(False),"servers":[mcp_public(r) for r in list_records("mcp_config")]}

# 连接检查也作为 Job 发给本地执行器，避免平台为了检查 MCP 直接运行外部命令。
def probe_start(identity):
    row=read_record(identity,"mcp_config")
    with session() as db:
        pending=db.scalar(select(Record).where(Record.kind=="mcp_probe",Record.project_id==current_project(),Record.status.in_(["queued","running"])))
        if pending:raise ValueError("已有 MCP 连接检查正在进行，请稍后")
        payload=dict(id=uid("PROBE"),serverId=identity,revision=row["revision"],status="queued",summary="等待检查连接",createdAt=stamp())
        db.add(Record(id=payload["id"],kind="mcp_probe",status="queued",payload=payload))
        db.add(Job(id=uid("JOB"),kind="local" if settings.role=="platform" else "agent",record_id=payload["id"]))
    return payload

def probe_execute(identity):
    from .mcp_client import MCPHub
    probe=read_record(identity,"mcp_probe")
    row=read_record(probe["serverId"],"mcp_config")
    if row["revision"]!=probe["revision"]:
        update_record(identity,lambda p:p.update(status="error",summary="配置已修改，请重新检查"))
        return
    with MCPHub([row],owner=identity) as hub:
        status=hub.statuses.get(row["id"],{})
        update_record(identity,lambda p:p.update(status="completed" if status.get("status")=="connected" else "error",summary=status.get("error") or f"连接成功，发现 {len(status.get('tools',[]))} 个工具"))
