"""同源执行器入口。API 验证浏览器身份，转发到固定内部地址；不接受任意代理 URL。"""
import re
import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from .config import settings
from .deployment import require_admin

router = APIRouter(prefix="/api/runtime")


@router.api_route("/{path:path}", methods=["GET", "POST"])
async def runtime(path: str, request: Request):
    if not settings.managed_executor: raise HTTPException(409, "当前部署仍使用旧的手动执行器，请更新安装配置")
    # 有限的路由白名单防止浏览器访问配对接口、任意 URL 或执行任意安装命令。
    allowed = {"GET": r"(?:status|catalog|models|components|components/[A-Za-z0-9_-]+)",
               "POST": r"(?:environment|models|models/[A-Za-z0-9_-]+/(?:delete|test)|components/install)"}
    if not re.fullmatch(allowed[request.method], path): raise HTTPException(404, "找不到运行环境接口")
    if path == "components/install": require_admin(request.state.user)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 32_768: raise HTTPException(413, "运行环境请求过大")
    headers = {"Authorization": "Bearer " + settings.managed_token,
               "X-AIQA-Managed-Account": request.state.user["id"], "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=70, follow_redirects=False) as client:
            response = await client.request(request.method, settings.runtime_url.rstrip("/") + "/" + path,
                                            headers=headers, content=bytes(data))
        return JSONResponse(response.json(), status_code=response.status_code)
    except (httpx.RequestError, ValueError):
        # 不打印含供应商 Key 的请求对象或原始错误。
        return JSONResponse({"message": "执行服务不可用，请检查安装状态和执行器日志"}, status_code=503)
