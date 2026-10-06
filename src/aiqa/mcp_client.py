# AIQA 作为 MCP 客户端：发现外部工具，将 schema 注册给 Agent，再转发实际调用。
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
def tool_name(row, name):
    slug=re.sub(r"[^a-zA-Z0-9_-]","_",name)[:28] or "tool"
    server=re.sub(r"[^a-zA-Z0-9_-]+","_",row["name"]).strip("_")[:14] or "server"
    return "mcp_"+server+"_"+row["id"][-6:].lower()+"_"+slug+"_"+hashlib.sha256(name.encode()).hexdigest()[:5]

class MCPHub:
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

    def scrub(self, text):
        from .runners.container import redact
        text=redact(text)
        for value in sorted(set(self.secrets),key=len,reverse=True):
            if value:text=text.replace(value,"[REDACTED]")
        return text

    def _remember_credentials(self, headers, env=None):
        self.secrets += list(headers.values()) + list((env or {}).values())
        for key,value in headers.items():
            if key.lower() not in {"authorization","proxy-authorization"}:
                continue
            match=re.fullmatch(r"(Bearer|Token|Basic)\s+(.+)",value,re.IGNORECASE)
            if not match:continue
            opaque=match[2]
            # A server may echo only the token, rather than the whole header.
            self.secrets.append(opaque)
            if match[1].lower()=="basic":
                try:
                    decoded=base64.b64decode(opaque,validate=True).decode("utf-8")
                    self.secrets.append(decoded)
                    if ":" in decoded:self.secrets.append(decoded.split(":",1)[1])
                except (ValueError,UnicodeError):pass

    def scrub_value(self,value):
        if isinstance(value,str):return self.scrub(value)
        if isinstance(value,list):return [self.scrub_value(v) for v in value]
        if isinstance(value,dict):return {k:self.scrub_value(v) for k,v in value.items()}
        return value

    def _status(self,row,status,tools=None,error=""):
        result=dict(status=status,tools=tools or [],error=self.scrub(error)[:1000],checkedAt=stamp())
        self.statuses[row["id"]]=result
        try:
            update_record(row["id"],lambda p:p.update(**result) if p["revision"]==row["revision"] else None)
        except ValueError:pass

    # 按 stdio/SSE/Streamable HTTP 建立协议会话，initialize 后分页 list_tools。
    # 同时限制工具数量/schema 大小并检查格式；接通服务器还不意味着其工具可无限制执行。
    async def _connect(self,row,stack):
        secret=credentials(row)
        self._remember_credentials(secret["headers"],secret["env"])
        headers={}
        for key,value in secret["headers"].items():
            def replacement(match):
                if match[1] not in secret["env"]:raise ValueError("请求头引用了未提供的环境变量")
                return secret["env"][match[1]]
            headers[key]=re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",replacement,value)
        self._remember_credentials(headers)
        if row["transport"]=="stdio":
            network={"targets":settings.target_network,"internet":"bridge","none":"none"}[row["network"]]
            config=dict(image=row["image"],command=row["command"],args=row["args"],env=secret["env"],network=network,owner=self.owner,ttl=settings.task_timeout+120 if self.owner.startswith("TASK-") else 180)
            env={"AIQA_MCP_BRIDGE_CONFIG":json.dumps(config),"PYTHONPATH":"/app/src"}
            transport=await stack.enter_async_context(stdio_client(
                StdioServerParameters(command=sys.executable,args=["-m","aiqa.mcp_bridge"],env=env),
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
    async def _server(self,row,started,queue):
        # A separate owner task per connection contains AnyIO transport cancellation.
        # Every transport/session context is entered and exited by this same task.
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

    def _thread_main(self):
        try:asyncio.run(self._serve())
        except BaseException as error:
            failure=RuntimeError("MCP 会话意外中断："+type(error).__name__)
            if not self.ready.done():self.ready.set_exception(failure)
            if not self.closed.done():self.closed.set_exception(failure)

    def __enter__(self):
        if not self.rows:return self
        self.thread=threading.Thread(target=self._thread_main,daemon=True)
        self.thread.start()
        try:self.ready.result(timeout=30)
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self,*_):
        if self.thread and self.loop and not self.closed.done():
            self.loop.call_soon_threadsafe(self.shutdown.set)
            try:self.closed.result(timeout=15)
            except Exception:pass
        if self.thread:self.thread.join(timeout=1)

    # Agent 的同步工具通过 Future 等待异步结果；不是同步代码直接乱入另一个事件循环。
    # 外部 MCP 也必须检查本次 Agent 的执行许可。
    def call(self,alias,arguments):
        if self.owner.startswith("TASK-"):
            from .agent.permissions import ensure_permit
            ensure_permit(self.owner,alias,arguments)
        if self.closed.done() or not self.loop:raise ToolException("MCP 会话已经结束")
        future=concurrent.futures.Future()
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
    def tools(self):
        result=[]
        for alias,schema,description in self.definitions:
            def make_invoke(tool_alias):
                def invoke(**arguments):return self.call(tool_alias,arguments)
                return invoke
            invoke=make_invoke(alias)
            result.append(StructuredTool(name=alias,args_schema=schema,description=description,func=invoke))
        return result


def cleanup_mcp_containers():
    """Recover isolated servers left behind if a trusted Worker process dies."""
    import time
    import docker
    from sqlalchemy import select
    from .storage import session,Record,Job,now
    client=docker.from_env()
    try:
        for container in client.containers.list(all=True,filters={"label":"aiqa.mcp=true"}):
            owner=container.labels.get("aiqa.mcp.owner","")
            expired=float(container.labels.get("aiqa.mcp.deadline","0"))<time.time()
            with session() as db:
                row=db.get(Record,owner)
                job=db.scalar(select(Job).where(Job.record_id==owner))
                if row and row.status not in {"queued","running"} and (now()-row.updated_at).total_seconds()>15:expired=True
                if job and job.status=="running" and job.lease and job.lease<now():expired=True
            if expired:
                try:container.remove(force=True)
                except docker.errors.NotFound:pass
    finally:client.close()
