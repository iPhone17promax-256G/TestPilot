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
from .storage import Record, session, list_records, read_record, update_record, uid, assert_project
from .job_queue import enqueue
from .workspace import current_project

BUILTINS = {
    "api-testing": "HTTPX 接口测试：鉴权、参数边界、业务断言与数据清理。",
    "web-testing": "观察页面、稳定定位、Playwright 自动等待与 UI 断言。",
    "performance-testing": "JMeter 计划、低负载联调、压测指标与阈值。",
    "app-testing": "Android / Appium：设备检查、页面观察与回归验证。",
    "failure-analysis": "根据报告、日志和截图分析测试失败。",
    "testpilot-guide": "TestPilot 用例版本、运行、报告与工具使用方法。",
}
SKILL_ROOT = Path("/app/skills")
MAX_SERVERS = 8

# 功能：复用测试器的时间格式生成扩展记录更新时间，保持平台各类记录的格式一致。
# 输入：无参数；在调用时导入 testing.stamp，避免模块加载时产生相互导入问题。
# 输出/影响：返回格式化时间字符串；不读取或写入扩展记录。
def stamp():
    from .testing import stamp as format_stamp
    return format_stamp()

# 功能：校验 Skill 资源名只指向包内的安全相对文件，防止借资源路径访问外部文件。
# 输入：name 来自上传 ZIP 成员名或 skills_read.resource 请求；采用 POSIX 路径格式，不接受绝对路径、反斜线或上级目录。
# 输出/影响：返回规范相对名称字符串；非法输入抛 ValueError；只检查文本，不打开文件。
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
# 功能：解析 SKILL.md 的 YAML 头并核验技能名称、说明与非空正文，供技能目录显示。
# 输入：content 是完整 Markdown 文本，开头应有 --- 包围的 YAML name/description；不接受 YAML 别名或覆盖内置技能名。
# 输出/影响：返回（技能名，简介）；只解析内存文本，不保存或执行正文/脚本；格式和长度违规抛 ValueError。
def parse_skill(content):
    if not content.startswith("---\n") and not content.startswith("---\r\n"):
        raise ValueError("SKILL.md 需要 YAML 开头，包含 name 和 description")
    parts = re.split(r"^---\s*$", content, maxsplit=2, flags=re.MULTILINE)
    if len(parts)!=3 or len(parts[1])>4000:
        raise ValueError("Skill 元信息格式不正确或过长")
    # 拒绝 SKILL.md 的 YAML 锚点引用（alias），避免解析出递归结构；技能目录只需简单的名称、说明等字段。
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

# 功能：整理待保存 Skill 的正文与辅助资源，并检查单文件、总大小和资源路径限制。
# 输入：body 是已校验的 SkillInput，含 content、resources 字典；辅助资源只接受无 NUL 的 UTF-8 文本。
# 输出/影响：返回（名称，说明，包含 SKILL.md 的文件字典）；不写数据库或运行程序，保存由 skill_save 负责。
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

# 功能：把内部 Skill 载荷整理成列表或详情页可见信息，默认不返回全文。
# 输入：payload 是 skill Record 的字典；detail 指示是否连同 content/files 返回资源正文。
# 输出/影响：返回新的公开字典，标 builtin=False 并列出资源名；只组装返回值，不改原记录。
def skill_public(payload, detail=False):
    result={k:payload[k] for k in ["id","name","description","enabled","revision","updatedAt"] if k in payload}
    result.update(builtin=False,resources=sorted(payload["files"]))
    if detail:result.update(content=payload["files"]["SKILL.md"],files=payload["files"])
    return result

# 功能：合并内置与当前项目自定义 Skill 的名称/简介目录，用于展示及本轮 Agent 按需选择。
# 输入：enabled_only 默认为 True，只保留启用的自定义 Skill；内置项从 BUILTINS 构造，项目范围由当前上下文决定。
# 输出/影响：返回目录字典列表；读取 skill 记录但不读取全部内置正文，也不执行任何技能脚本。
def skills_catalog(enabled_only=True):
    result=[dict(id=name,name=name,description=description,enabled=True,builtin=True,revision=1,resources=["SKILL.md"]) for name,description in BUILTINS.items()]
    result += [skill_public(row) for row in list_records("skill") if not enabled_only or row["enabled"]]
    return result

# 功能：在当前项目创建或更新自定义 Skill，记录资源正文、启用状态及递增版本。
# 输入：body 是页面/导入构造的 SkillInput，id 为空新建，否则更新指定记录；content/resources 先经过 validate_skill。
# 输出/影响：返回公开详情；事务锁项目、检查同名及数量，写 kind=skill 的 Record.payload，正文资源存在数据库 JSON 中，不启动脚本。
def skill_save(body):
    name, description, files = validate_skill(body)
    with session() as db:
        # 锁住当前项目的数据库记录后，再检查扩展名称是否重复和数量是否超限，避免同一项目的并发导入绕过限制。
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
# 功能：把 Markdown 或 ZIP 上传解析为 SkillInput，验证后交给统一保存入口。
# 输入：filename 是用户上传名称；data 是上传原始字节；ZIP 支持根目录或单层包目录的 SKILL.md 及文本资源。
# 输出/影响：返回 skill_save 的公开详情；将文字保存为 skill 记录，不执行上传脚本；拒绝过大、链接、加密、重复成员和错误编码。
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
# 功能：按需读取已启用 Skill 的说明或资源的一段文本，避免一次把全部材料送给模型。
# 输入：name 是技能名称；resource 是包内相对路径；offset/limit 以字符计，来自工具参数或阅读请求。
# 输出/影响：返回正文片段、资源列表、版本、总字符数和 nextOffset；内置读 /app/skills，自定义读当前项目 skill 记录；不执行资源中的程序。
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

# 功能：删除当前项目的一条 Skill 或 MCP 配置，统一检查种类和项目归属。
# 输入：identity 是扩展 Record ID；kind 是调用者指定的 skill/mcp_config 等记录种类。
# 输出/影响：返回 status=deleted 字典；在事务中删除记录；不关闭已经启动的本轮连接或删除模型 Key。
def delete_extension(identity, kind):
    with session() as db:
        row=db.get(Record,identity)
        if not row or row.kind!=kind:raise ValueError("扩展不存在")
        assert_project(row)
        db.delete(row)
    return {"status":"deleted"}

# MCP 连接凭据先用 Fernet 加密再保存到平台扩展记录；解密主密钥在平台 settings.data 的私有文件中，掌握平台数据库和主密钥的管理员仍能解密。
# 模型供应商 API Key 由执行器的 LocalVault/ServerVault 保存，MCP 凭据由平台保存并授权下发；二者存放位置不同，不能把“模型 Key 不进平台记录”推广为所有凭据都不进平台。
# 功能：获取用于 MCP 连接凭据的 Fernet 加解密器，缺失主密钥时安全创建平台私有文件。
# 输入：无参数；主密钥位置为当前进程 settings.data/extension-secrets.key，临时文件以限制权限创建。
# 输出/影响：返回 Fernet 对象；可能创建主密钥文件，并发时通过文件链接避免覆盖；不是模型供应商 Key 的保存器。
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

# 功能：将 MCP 请求头/环境变量凭据序列化后加密，供平台扩展记录保存。
# 输入：values 是 headers/env 等凭据字典；加密主密钥由 cipher 从平台私有文件取得。
# 输出/影响：返回 Fernet 密文字符串；可能初始化主密钥文件，但不自行写 MCP Record；实际持久化由 mcp_save 完成。
def seal(values):
    return cipher().encrypt(json.dumps(values).encode()).decode()

# 功能：取出 MCP 连接所需的明文凭据，在授权执行端快照与平台加密记录之间选择来源。
# 输入：row 是 MCP 配置载荷；executor 角色可读取已下发的 localCredentials，否则解密 credentials 字段。
# 输出/影响：返回 headers/env 字典，缺失时为空字典；可能读取平台主密钥文件；不返回给公开页面或把模型 Key 当 MCP 凭据。
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
    # 省略镜像时沿用本次部署的测试执行镜像，不能把开发环境的标签写死到成品安装中。
    image: str = Field(default_factory=lambda: settings.runner_image,min_length=1,max_length=200)
    network: str = Field(default="targets",pattern=r"^(targets|internet|none)$")
    headers: dict[str,str]|None = None
    env: dict[str,str]|None = None
    clear_credentials: bool = False
    enabled: bool = True
    timeout: int = Field(default=30,ge=5,le=120)

    # 功能：在 Pydantic 完成字段校验后检查 MCP 传输地址、入口命令和凭据格式的组合约束。
    # 输入：self 是从配置表单/导入构造的 MCPInput；stdio 用 command/image/args，HTTP 或 SSE 用无内嵌凭据的 URL。
    # 输出/影响：返回已校验 self；非法组合抛 ValueError；只验证配置，不发请求、启动容器或保存密钥。
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

# 功能：从内部 MCP 配置生成页面可见信息，隐藏凭据值但列出已配置的键名。
# 输入：row 是 MCP Record.payload；内部 credentials 用于取得 headers/env 的键集合。
# 输出/影响：返回去掉 credentials/projectId 的新字典并添加 credentialKeys；读取凭据键名，不改记录；本函数用于平台原始配置，执行器下发的 localCredentials 快照须另行处理。
def mcp_public(row):
    result={k:v for k,v in row.items() if k not in {"credentials","projectId"}}
    secret=credentials(row)
    result["credentialKeys"]={k:sorted(v) for k,v in secret.items()}
    return result

# 功能：新建或更新当前项目的 MCP 连接及加密凭据，重置连接检查状态并递增版本。
# 输入：body 是 MCPInput；id 决定新建/更新，headers/env=None 保留旧值，clear_credentials 清空后再合并本次提交。
# 输出/影响：返回公开配置；事务写 kind=mcp_config 的 Record，密文存 credentials，主密钥在平台私有文件；不在保存时连接 MCP。
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
# 功能：把常见 mcpServers 配置转换为当前项目的 MCP 记录，复用单连接校验与保存流程。
# 输入：configuration 是导入 JSON 字典；每项包含 HTTP URL 或 stdio 命令、镜像、网络和凭据等字段；不支持宿主 cwd。
# 输出/影响：返回保存的公开配置列表；写数据库及加密凭据；先验证各项但保存逐项提交，不是整份导入的单一事务，不启动外部程序。
def mcp_import(configuration):
    servers=configuration.get("mcpServers")
    if not isinstance(servers,dict) or not servers:raise ValueError("配置需要 mcpServers 对象")
    if len(list_records("mcp_config"))+len(servers)>MAX_SERVERS:raise ValueError("每个项目最多 8 个 MCP 连接")
    bodies=[]
    names={r["name"] for r in list_records("mcp_config")}
    for name,entry in servers.items():
        if not isinstance(entry,dict):raise ValueError("MCP 配置项必须是对象")
        if name in names:raise ValueError("已存在同名 MCP："+name)
        # 导入 stdio MCP 时，遇到依赖宿主机工作目录或不支持的启动命令就明确拒绝；不能静默删掉 cwd/命令字段后保存一个实际运行行为不同的连接。
        if any(k in entry for k in ["cwd","workingDirectory"]):raise ValueError("stdio 使用隔离容器，不支持主机 cwd；请配置容器镜像内的路径")
        transport=entry.get("transport") or entry.get("type") or ("stdio" if "command" in entry else "streamable-http")
        transport={"http":"streamable-http","streamable_http":"streamable-http"}.get(transport,transport)
        fields={k:entry[k] for k in ["url","command","args","headers","env","image","network","timeout","enabled"] if k in entry}
        bodies.append(MCPInput(name=name,transport=transport,**fields))
    return [mcp_save(body) for body in bodies]

# 功能：汇总当前项目所有 Skill 与 MCP 的公开状态，供页面刷新扩展管理数据。
# 输入：无显式参数；项目归属从上下文取得；skills_catalog(False) 包含禁用自定义技能。
# 输出/影响：返回 skills/servers 字典；读取平台配置并公开 MCP 凭据键名，隐藏 credentials 字段，不加载全部技能正文；不用于公开执行器的 localCredentials 快照。
def extension_snapshot():
    return {"skills":skills_catalog(False),"servers":[mcp_public(r) for r in list_records("mcp_config")],
            "defaults":{"stdioImage":settings.runner_image}}

# MCP 连接检查也创建 Job 交给执行器，平台 API 不直接启动 MCP 外部命令；执行服务的部署位置由本地/服务端模式决定。
# 功能：创建 MCP 连接检查记录和排队作业，让执行端负责连接探测而非平台 API 直接启动命令。
# 输入：identity 是待检查 MCP 配置 ID；会固定其 revision，并检查当前项目是否已有排队/运行中的探测。
# 输出/影响：返回 mcp_probe 载荷；事务写 Record 和 Job；平台角色建 local 类型作业，兼容分支建 agent 作业，不在此真正连接 MCP。
def probe_start(identity):
    row=read_record(identity,"mcp_config")
    with session() as db:
        pending=db.scalar(select(Record).where(Record.kind=="mcp_probe",Record.project_id==current_project(),Record.status.in_(["queued","running"])))
        if pending:raise ValueError("已有 MCP 连接检查正在进行，请稍后")
        payload=dict(id=uid("PROBE"),serverId=identity,revision=row["revision"],status="queued",summary="等待检查连接",createdAt=stamp())
        db.add(Record(id=payload["id"],kind="mcp_probe",status="queued",payload=payload))
        enqueue(db,db.get(Record,payload["id"]),"local")
    return payload

# 功能：执行指定 MCP 配置版本的连接探测，并把发现的工具数或失败原因保存到探测记录。
# 输入：identity 是已领取的 mcp_probe ID；从其 serverId/revision 读取连接；配置已改变则拒绝沿用旧探测。
# 输出/影响：返回 None；MCPHub 可能网络连接或启动受限 stdio 容器，with 退出回收；更新 mcp_probe 的 completed/error 和 summary。
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
