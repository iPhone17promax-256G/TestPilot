"""Reject plaintext platform traffic before authentication or request-body parsing."""

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequireHTTPSMiddleware:
    """平台和执行器共用的传输边界；不把客户端自报的代理请求头当作 TLS 证明。"""

    # 功能：保存下游 ASGI 应用，不访问数据库、证书文件或请求正文。
    # 输入：app 是后续认证、路由及挂载应用；没有允许 HTTP 的配置开关。
    # 输出/影响：构造可复用中间件；部署入口还必须真正启用 TLS，并禁用未受信代理头解析。
    def __init__(self, app: ASGIApp):
        self.app = app

    # 功能：在认证与路由前拒绝 HTTP/WS，允许真实 HTTPS/WSS，并为 HTTPS 响应补安全头。
    # 输入：scope.scheme 来自 ASGI 服务器的连接状态；receive/send 是本次连接的读写通道。
    # 输出/影响：不安全 HTTP 返回固定 426 JSON、不读取密码正文；不安全 WS 在握手前关闭。
    # 数据：不保存请求内容、登录凭据或访问记录；Forwarded/X-Forwarded-Proto 不参与判断。
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "http" and scope.get("scheme") != "https":
            response = JSONResponse(
                {"message": "TestPilot 仅允许 HTTPS 访问，请使用 https:// 地址；未加密的 HTTP 请求已拒绝。"},
                status_code=426,
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Upgrade": "TLS/1.2, TLS/1.3",
                },
            )
            await response(scope, receive, send)
            return
        if kind == "websocket" and scope.get("scheme") != "wss":
            await send({"type": "websocket.close", "code": 1008, "reason": "TestPilot 仅允许 WSS 加密连接"})
            return

        # 功能：在 HTTPS 的响应开始时补 HSTS，不改变响应正文、状态码或业务 Cookie。
        # 输入：message 是下游产生的一条 ASGI 响应消息；HTTP 之外的生命周期和 WebSocket 消息直接通过。
        # 输出/影响：HSTS 仅作用于本主机，不包含 includeSubDomains/preload，避免扩大本地证书的影响范围。
        async def secure_send(message: Message) -> None:
            if kind == "http" and message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Strict-Transport-Security", "max-age=31536000")
                headers.setdefault("X-Content-Type-Options", "nosniff")
            await send(message)

        await self.app(scope, receive, secure_send)
