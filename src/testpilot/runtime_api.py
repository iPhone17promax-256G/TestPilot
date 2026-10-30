"""同源执行器入口。API 验证浏览器身份，转发到固定内部地址；不接受任意代理 URL。"""
import re
import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from .config import settings, require_https_url
from .deployment import require_admin

router = APIRouter(prefix="/api/runtime")


# 功能：代理有限的运行环境请求到受管执行服务。
# 输入：path 是白名单子路径，request 提供方法/账号/请求流；安装配置提供内部地址和令牌。
# 输出/影响：GET/POST 经 AsyncClient 转发并返回执行服务 JSON；安装组件需管理员，限制 32 KB，连接失败返回 503。
@router.api_route("/{path:path}", methods=["GET", "POST"])
async def runtime(path: str, request: Request):
    if not settings.managed_executor: raise HTTPException(409, "当前部署仍使用旧的手动执行器，请更新安装配置")
    # 转发包含内部身份和可能的模型 Key，地址错误时必须在发送前拒绝。
    runtime_url=require_https_url(settings.runtime_url,"执行服务通信地址")
    # 有限的路由白名单防止浏览器访问配对接口、任意 URL 或执行任意安装命令。
    allowed = {"GET": r"(?:status|catalog|models|knowledge|components|components/[A-Za-z0-9_-]+)",
               "POST": r"(?:environment|models|knowledge|knowledge/test|models/[A-Za-z0-9_-]+/(?:delete|test)|components/install)"}
    if not re.fullmatch(allowed[request.method], path): raise HTTPException(404, "找不到运行环境接口")
    if path == "components/install": require_admin(request.state.user)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 32_768: raise HTTPException(413, "运行环境请求过大")
    headers = {"Authorization": "Bearer " + settings.managed_token,
               "X-TestPilot-Managed-Account": request.state.user["id"], "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=70, follow_redirects=False) as client:
            response = await client.request(request.method, runtime_url + "/" + path,
                                            headers=headers, params=request.query_params, content=bytes(data))
        return JSONResponse(response.json(), status_code=response.status_code)
    except (httpx.RequestError, ValueError):
        # 不打印含供应商 Key 的请求对象或原始错误。
        return JSONResponse({"message": "执行服务不可用，请检查安装状态和执行器日志"}, status_code=503)
