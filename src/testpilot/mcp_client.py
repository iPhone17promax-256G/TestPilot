# TestPilot 作为 MCP 客户端：发现外部工具，将 schema 注册给 Agent，再转发实际调用。
# 同步 Agent 与异步 MCP 传输之间用专门线程和队列衔接，连接生命周期归该线程内的异步任务管理。

"""MCP sessions live for an Agent turn; all AnyIO scopes stay in one async task."""
import asyncio
import base64
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import threading
from contextlib import AsyncExitStack
from datetime import timedelta
import httpx
from jsonschema import Draft202012Validator
from langchain_core.tools import StructuredTool, ToolException
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from .config import settings
from .storage import read_record, update_record
from .workspace import current_project, project_scope
from .extensions import credentials, stamp

# 给不同服务器上的同名工具生成稳定别名，避免多个 MCP 都有 search 时覆盖彼此。
# 功能：为外部 MCP 工具生成稳定且可区分服务器的 Agent 工具别名。
# 输入：row 含 MCP 配置 id/name；name 为服务端原始工具名，可能与其他服务重名。
# 输出/影响：返回 mcp_ 前缀别名，含配置 ID 后缀和名称 hash；仅字符串处理，不注册或调用工具。
def tool_name(row, name):
    slug=re.sub(r"[^a-zA-Z0-9_-]","_",name)[:28] or "tool"
    server=re.sub(r"[^a-zA-Z0-9_-]+","_",row["name"]).strip("_")[:14] or "server"
    return "mcp_"+server+"_"+row["id"][-6:].lower()+"_"+slug+"_"+hashlib.sha256(name.encode()).hexdigest()[:5]

class MCPHub:
    # 功能：初始化一轮 Agent 所需的 MCP 会话管理器，准备线程同步状态与工具目录。
    # 输入：rows 为已选择的 MCP 配置记录列表；owner 为 Task ID 或 probe；当前项目从 ContextVar 获取。
    # 输出/影响：构造返回 None；只初始化内存，连接和线程在 __enter__ 才启动，密钥不写入新文件。
    def __init__(self, rows, owner="probe"):
        self.rows=rows
        self.owner=owner
        self.project=current_project()
        self.statuses={}
        self.definitions=[]
        self.ready=concurrent.futures.Future()
        self.closed=concurrent.futures.Future()
        self.secrets=[]
        self.loop=None
        self.queue=None
        self.thread=None

    # 功能：隐藏外部 MCP 输出中已知模型、测试和 MCP 连接凭据。
    # 输入：text 为输出或错误文本；self.secrets 是本轮连接时记下的凭据，另使用 Runner 的 redact 规则。
    # 输出/影响：返回替换后的字符串；不修改工具远端结果或持久化秘密，不能识别未登记的敏感内容。
    def scrub(self, text):
        from .runners.container import redact
        text=redact(text)
        for value in sorted(set(self.secrets),key=len,reverse=True):
            if value:text=text.replace(value,"[REDACTED]")
        return text

    # 功能：登记连接凭据及 Authorization 的裸 Token/Basic 解码值，以便日志清理。
    # 输入：headers 为本次 MCP HTTP 头字典；env 为可选 stdio 容器环境变量字典。
    # 输出/影响：返回 None；只把秘密放入 self.secrets 内存，解析失败忽略，不写数据库或打印凭据。
    def _remember_credentials(self, headers, env=None):
        self.secrets += list(headers.values()) + list((env or {}).values())
        for key,value in headers.items():
            if key.lower() not in {"authorization","proxy-authorization"}:
                continue
            match=re.fullmatch(r"(Bearer|Token|Basic)\s+(.+)",value,re.IGNORECASE)
            if not match:continue
            opaque=match[2]
            # MCP 服务报错可能只回显 Bearer 后面的令牌值，未包含完整 Authorization 头；因此清理规则也记录该令牌本身，才能在错误文字中替换它。
            self.secrets.append(opaque)
            if match[1].lower()=="basic":
                try:
                    decoded=base64.b64decode(opaque,validate=True).decode("utf-8")
                    self.secrets.append(decoded)
                    if ":" in decoded:self.secrets.append(decoded.split(":",1)[1])
                except (ValueError,UnicodeError):pass

    # 功能：递归应用文本凭据清理，用于 MCP schema 和结构化返回值。
    # 输入：value 为字符串、列表、字典或其他标量；已知秘密来自当前 Hub。
    # 输出/影响：返回清理后的结构，其他类型保留；不修改原数据，不运行工具。
    def scrub_value(self,value):
        if isinstance(value,str):return self.scrub(value)
        if isinstance(value,list):return [self.scrub_value(v) for v in value]
        if isinstance(value,dict):return {k:self.scrub_value(v) for k,v in value.items()}
        return value

    # 功能：更新 MCP 检查/连接状态，同时防止旧连接覆盖用户已修改的配置版本。
    # 输入：row 为连接时的配置快照；status 为状态；tools 为发现的元信息；error 为可选错误文字。
    # 输出/影响：返回 None；self.statuses 保存内存摘要，版本仍一致时更新 mcp_config 业务记录，错误先脱敏限长。
    def _status(self,row,status,tools=None,error=""):
        result=dict(status=status,tools=tools or [],error=self.scrub(error)[:1000],checkedAt=stamp())
        self.statuses[row["id"]]=result
        try:
            update_record(row["id"],lambda p:p.update(**result) if p["revision"]==row["revision"] else None)
        except ValueError:pass

    # 按 stdio/SSE/Streamable HTTP 建立协议会话，initialize 后分页 list_tools。
    # 同时限制工具数量/schema 大小并检查格式；接通服务器还不意味着其工具可无限制执行。
    # 功能：按 stdio/SSE/Streamable HTTP 建立 MCP 会话并分页发现校验工具。
    # 输入：row 含传输、地址/镜像/命令、网络和超时；stack 为拥有该连接的 AsyncExitStack，凭据由 credentials 读取。
    # 输出/影响：异步返回 (ClientSession, 工具列表)；真实连接或启动隔离 stdio 服务，限制工具/schema，清理由同一 stack 负责。
    async def _connect(self,row,stack):
        secret=credentials(row)
        self._remember_credentials(secret["headers"],secret["env"])
        headers={}
        for key,value in secret["headers"].items():
            # 功能：替 MCP 请求头中的 ${环境变量} 填入已提供的连接凭据。
            # 输入：match 为 re.sub 正则匹配对象；变量值来自 _connect 外层 secret.env。
            # 输出/影响：返回对应字符串，缺变量抛 ValueError；只组装内存头值，不读任意宿主环境或保存秘密。
            def replacement(match):
                if match[1] not in secret["env"]:raise ValueError("请求头引用了未提供的环境变量")
                return secret["env"][match[1]]
            headers[key]=re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",replacement,value)
        self._remember_credentials(headers)
        if row["transport"]=="stdio":
            network={"targets":settings.target_network,"internet":"bridge","none":"none"}[row["network"]]
            config=dict(image=row["image"],command=row["command"],args=row["args"],env=secret["env"],network=network,owner=self.owner,ttl=settings.task_timeout+120 if self.owner.startswith("TASK-") else 180)
            env={"TESTPILOT_MCP_BRIDGE_CONFIG":json.dumps(config),"PYTHONPATH":"/app/src"}
            transport=await stack.enter_async_context(stdio_client(
                StdioServerParameters(command=sys.executable,args=["-m","testpilot.mcp_bridge"],env=env),
                errlog=stack.enter_context(open(os.devnull,"w"))))
        elif row["transport"]=="sse":
            transport=await stack.enter_async_context(sse_client(row["url"],headers=headers,timeout=10,sse_read_timeout=row["timeout"]))
        else:
            client=await stack.enter_async_context(httpx.AsyncClient(headers=headers,timeout=httpx.Timeout(10,read=row["timeout"])))
            transport=await stack.enter_async_context(streamable_http_client(row["url"],http_client=client))
        session=await stack.enter_async_context(ClientSession(transport[0],transport[1],read_timeout_seconds=timedelta(seconds=row["timeout"])))
        await session.initialize()
        tools=[]
        cursor=None
        for _ in range(4):
            page=await session.list_tools(cursor=cursor)
            tools.extend(page.tools)
            if len(tools)>60:raise ValueError("每个 MCP 最多接入 60 个工具")
            cursor=page.nextCursor
            if not cursor:break
        if cursor:raise ValueError("MCP 工具分页过多")
        names=set()
        for tool in tools:
            if tool.name in names:raise ValueError("MCP 返回重复工具名称")
            names.add(tool.name)
            tool.inputSchema=self.scrub_value(tool.inputSchema)
            schema=tool.inputSchema
            # 功能：递归检查 MCP 输入 schema，只允许 JSON Schema 的内部引用。
            # 输入：value 是工具 inputSchema 的当前节点，可为字典、列表或标量，由 _connect 调用。
            # 输出/影响：返回 None；发现外部 $ref 抛错，避免验证阶段访问外部引用，不修改 schema。
            def check_refs(value):
                if isinstance(value,dict):
                    if "$ref" in value and not str(value["$ref"]).startswith("#"):raise ValueError("MCP schema 仅支持内部引用")
                    for item in value.values():check_refs(item)
                elif isinstance(value,list):
                    for item in value:check_refs(item)
            check_refs(schema)
            if schema.get("type","object")!="object" or len(json.dumps(schema))>18000:raise ValueError("MCP 工具参数需要有限长度的 JSON 对象")
            Draft202012Validator.check_schema(schema)
        return session,tools

    # 真正调用前复查扩展仍启用且版本未变，验证参数 schema，再以超时限制请求。
    # 工具结果进行大小限制和已知凭据清理；isError 必须作为失败传回模型，不能当正常成功。
    # 功能：实际执行 MCP 工具前复查启用/版本/任务状态和参数，再限时请求并处理结果。
    # 输入：row 为连接配置快照；session 为已初始化 ClientSession；tool 为原始工具定义；arguments 为模型工具参数字典。
    # 输出/影响：异步返回限长脱敏文本/结构化/可选图片；请求外部服务，isError 抛 ToolException，具体外部影响由工具决定。
    async def _call(self,row,session,tool,arguments):
        current=read_record(row["id"],"mcp_config")
        if not current["enabled"] or current["revision"]!=row["revision"]:
            raise ValueError("MCP 已关闭或配置已修改，请在下一轮对话重新连接")
        if self.owner.startswith("TASK-") and read_record(self.owner,"task")["status"] in {"cancelled","interrupted"}:
            raise ValueError("任务已停止")
        errors=list(Draft202012Validator(tool.inputSchema).iter_errors(arguments))
        if errors:raise ValueError("MCP 工具参数不符合 schema："+errors[0].message[:400])
        async with asyncio.timeout(row["timeout"]):
            result=await session.call_tool(tool.name,arguments=arguments)
        content=[]
        image=None
        for block in result.content:
            if block.type=="text":content.append(block.text)
            elif block.type=="image" and block.mimeType in {"image/png","image/jpeg","image/webp"} and len(block.data)<5_000_000:
                if image is None:image=(block.data,block.mimeType)
            elif block.type=="resource" and hasattr(block.resource,"text"):
                content.append(block.resource.text)
            else:content.append("[MCP 返回 "+block.type+" 内容]")
        payload=dict(server=row["name"],tool=tool.name,text=self.scrub("\n".join(content))[:32000],isError=result.isError)
        if result.structuredContent is not None:
            payload["structuredContent"]=self.scrub_value(result.structuredContent) if len(json.dumps(result.structuredContent))<32000 else {"truncated":True}
        if image:payload.update(image=image[0],mimeType=image[1])
        if result.isError:raise ToolException(json.dumps({k:v for k,v in payload.items() if k!="image"},ensure_ascii=False))
        return payload

    # 每个连接在同一异步任务中进入和退出 AnyIO 上下文，避免跨任务关闭引起取消域错误。
    # 队列把外层同步调用转交给拥有此会话的任务处理；一个连接失败不会冒充工具已成功。
    # 功能：在同一异步任务中拥有一个 MCP 连接的完整生命周期，串行消费该服务器调用队列。
    # 输入：row 为服务器配置；started 为就绪 Future；queue 收到 (别名, 参数, 结果 Future)，None 表示结束。
    # 输出/影响：异步返回 None；发现工具并建立别名映射、更新连接状态、回填调用结果，退出时关闭传输并拒绝残留请求。
    async def _server(self,row,started,queue):
        # 每个 MCP 连接由独立的异步 owner 任务管理，其 AnyIO 传输取消范围限制在该任务内，避免一个连接断开连带终止其他连接。
        # 同一连接的传输与 ClientSession 上下文都由该 owner 任务进入和退出，不能由另一个异步任务替它关闭取消域。
        with project_scope(self.project):
            try:
                async with AsyncExitStack() as stack:
                    async with asyncio.timeout(15):
                        session,tools=await self._connect(row,stack)
                    total_schema=sum(len(json.dumps(schema)) for _,schema,_ in self.definitions)+sum(len(json.dumps(t.inputSchema)) for t in tools)
                    if len(self.definitions)+len(tools)>60 or total_schema>90000:
                        raise ValueError("当前项目的 MCP 工具超过 60 个或 schema 总长度超过 90 KB，请减少连接或工具")
                    metadata=[]
                    mapped={}
                    for tool in tools:
                        alias=tool_name(row,tool.name)
                        description=self.scrub(tool.description or tool.name)[:700]
                        metadata.append(dict(name=tool.name,agentName=alias,description=description))
                        mapped[alias]=tool
                        self.channels[alias]=queue
                        self.definitions.append((alias,tool.inputSchema,f"外部 MCP [{row['name']}] 的 {tool.name}：{description}"))
                    self._status(row,"connected",metadata)
                    started.set_result(True)
                    while True:
                        command=await queue.get()
                        if command is None:break
                        alias,arguments,future=command
                        if future.cancelled():continue
                        try:
                            result=await self._call(row,session,mapped[alias],arguments)
                            if not future.cancelled():future.set_result(result)
                        except Exception as error:
                            if not future.cancelled():future.set_exception(ToolException(self.scrub(str(error) or type(error).__name__)[:1200]))
            except BaseException as error:
                self._status(row,"error",error=str(error) if isinstance(error,ValueError) else f"连接或工具发现失败（{type(error).__name__}）。检查地址、凭据、镜像和入口命令。")
            finally:
                if not started.done():started.set_result(False)
                for alias in list(self.channels):
                    if self.channels[alias] is queue:self.channels.pop(alias)
                while not queue.empty():
                    command=queue.get_nowait()
                    if command is not None and not command[2].done():command[2].set_exception(ToolException("MCP 连接已中断"))

    # 功能：协调本轮多个 MCP owner 任务，待各连接完成发现后通知同步侧就绪。
    # 输入：无显式参数；self.rows 为配置，ready/closed 为跨线程 Future，shutdown 为内部停止事件。
    # 输出/影响：异步返回 None；建立事件循环队列和子任务，停止时发终止标记并等待连接清理，不执行 Agent 主循环。
    async def _serve(self):
        self.loop=asyncio.get_running_loop()
        self.shutdown=asyncio.Event()
        self.channels={}
        queues=[asyncio.Queue() for _ in self.rows]
        started=[self.loop.create_future() for _ in self.rows]
        workers=[asyncio.create_task(self._server(row,future,queue)) for row,future,queue in zip(self.rows,started,queues)]
        await asyncio.gather(*started)
        self.ready.set_result(True)
        await self.shutdown.wait()
        for queue in queues:queue.put_nowait(None)
        await asyncio.gather(*workers,return_exceptions=True)
        self.closed.set_result(True)

    # 功能：在线程中运行 MCP 异步协调器，并把无法启动的异常传回同步等待方。
    # 输入：self 为本轮 Hub；由 __enter__ 创建的后台线程调用，无额外参数。
    # 输出/影响：返回 None；asyncio.run 管理线程内事件循环，意外中断时设置 ready/closed 的异常，不输出秘密。
    def _thread_main(self):
        try:asyncio.run(self._serve())
        except BaseException as error:
            failure=RuntimeError("MCP 会话意外中断："+type(error).__name__)
            if not self.ready.done():self.ready.set_exception(failure)
            if not self.closed.done():self.closed.set_exception(failure)

    # 功能：进入 with 时启动 MCP 专用线程，并等待有界的连接发现过程。
    # 输入：self.rows 为本轮所选 MCP 列表；调用者为 Agent 运行时或连接测试。
    # 输出/影响：返回 self；空列表直接返回，否则最多等 30 秒 ready，启动失败调用 __exit__ 清理后抛错。
    def __enter__(self):
        if not self.rows:return self
        self.thread=threading.Thread(target=self._thread_main,daemon=True)
        self.thread.start()
        try:self.ready.result(timeout=30)
        except Exception:
            self.__exit__()
            raise
        return self

    # 功能：离开 with 时通知异步 owner 任务收尾，并等待 MCP 线程退出。
    # 输入：self 为当前 Hub；额外位置参数是 with 自动给出的异常信息，本实现不读取。
    # 输出/影响：返回 None；跨线程发送 shutdown，等待有限秒数并 join，不吞掉调用者原本异常。
    def __exit__(self,*_):
        if self.thread and self.loop and not self.closed.done():
            self.loop.call_soon_threadsafe(self.shutdown.set)
            try:self.closed.result(timeout=15)
            except Exception:pass
        if self.thread:self.thread.join(timeout=1)

    # Agent 的同步工具通过 Future 等待异步结果；不是同步代码直接乱入另一个事件循环。
    # 外部 MCP 也必须检查本次 Agent 的执行许可。
    # 功能：将同步 Agent 工具调用交给所属 MCP 异步任务，等待结果前检查当前执行许可。
    # 输入：alias 为已注册的 Agent 别名；arguments 为工具参数字典；owner 为 Task 时核对该次调用许可。
    # 输出/影响：返回 MCP 处理后的结果或抛 ToolException；跨线程队列/Future 最多等 130 秒，不直接在同步侧操作会话。
    def call(self,alias,arguments):
        if self.owner.startswith("TASK-"):
            from .agent.permissions import ensure_permit
            ensure_permit(self.owner,alias,arguments)
        if self.closed.done() or not self.loop:raise ToolException("MCP 会话已经结束")
        future=concurrent.futures.Future()
        # 功能：在 MCP 事件循环线程中把本次同步请求入对应服务器队列。
        # 输入：无显式参数；alias/arguments/future 来自外层 call，self.channels 存工具别名到队列的映射。
        # 输出/影响：返回 None；队列加入请求，连接已断开则设置结果 Future 异常，不在此调用远端工具。
        def submit():
            queue=self.channels.get(alias)
            if queue is None:future.set_exception(ToolException("MCP 连接已中断"))
            else:queue.put_nowait((alias,arguments,future))
        self.loop.call_soon_threadsafe(submit)
        try:return future.result(timeout=130)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise ToolException("MCP 工具调用超时") from None

    # 把 MCP JSON Schema 包装成 LangChain StructuredTool。
    # 闭包工厂固定每个工具别名，避免循环结束后所有函数都调用最后一个工具。
    # 功能：把已发现的 MCP 工具 schema 包装成 LangChain StructuredTool，供模型选择。
    # 输入：无显式参数；self.definitions 为别名、输入 schema、描述三元组，连接发现时填入。
    # 输出/影响：返回 StructuredTool 列表；只注册调用包装函数，不执行外部工具，真正执行通过 self.call。
    def tools(self):
        result=[]
        for alias,schema,description in self.definitions:
            # 功能：为当前循环中的一个工具固定别名，防止所有包装函数最后都指向同一个工具。
            # 输入：tool_alias 为本轮注册的别名；self 从 tools 外层捕获。
            # 输出/影响：返回 invoke 函数对象，不调用 MCP、不保存数据；每个返回函数拥有自己的别名闭包。
            def make_invoke(tool_alias):
                # 功能：作为 StructuredTool 的同步执行函数，把模型给出的参数交到固定 MCP 别名。
                # 输入：arguments 是工具 schema 校验后的关键字参数；tool_alias/self 来自 make_invoke 闭包。
                # 输出/影响：返回 self.call 的工具结果；此时才可能真实访问外部 MCP，并执行统一许可和限时检查。
                def invoke(**arguments):return self.call(tool_alias,arguments)
                return invoke
            invoke=make_invoke(alias)
            result.append(StructuredTool(name=alias,args_schema=schema,description=description,func=invoke))
        return result


# 功能：恢复清理可信执行器异常退出后遗留的隔离 stdio MCP 服务容器。
# 输入：无显式参数；读取 testpilot.mcp 标签、owner/deadline，以及关联任务和作业租约状态。
# 输出/影响：返回 None；期限已过、任务停止或租约失效时强制删除对应容器，最后关闭 Docker Client。
def cleanup_mcp_containers():
    """Recover isolated servers left behind if a trusted Worker process dies."""
    import time
    import docker
    from sqlalchemy import select
    from .storage import session,Record,now
    from .job_queue import get_job
    client=docker.from_env()
    try:
        for container in client.containers.list(all=True,filters={"label":"testpilot.mcp=true"}):
            owner=container.labels.get("testpilot.mcp.owner","")
            expired=float(container.labels.get("testpilot.mcp.deadline","0"))<time.time()
            with session() as db:
                row=db.get(Record,owner)
                job=get_job(db,owner)
                if row and row.status not in {"queued","running"} and (now()-row.updated_at).total_seconds()>15:expired=True
                if job and job.status=="running" and job.lease and job.lease<now():expired=True
            if expired:
                try:container.remove(force=True)
                except docker.errors.NotFound:pass
    finally:client.close()
