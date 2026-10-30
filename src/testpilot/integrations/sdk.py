# 外部调用的轻量 HTTP 客户端，CLI 和独立 stdio MCP 复用它。
# 账号 Token 证明调用者身份，X-TestPilot-Project 选择项目；它们不是模型供应商的 Key。

import os
import httpx
from ..config import require_https_url


class Client:
    # 功能：创建对外调用平台的 HTTPX 客户端，并配置账号认证与当前项目选择请求头。
    # 输入：url 可显式指定平台地址；否则取 TESTPILOT_URL；账号 Token 从 TESTPILOT_ACCESS_TOKEN 取，项目 ID 从 TESTPILOT_PROJECT_ID 取。
    # 输出/影响：返回 None，实例 self.http 保存 HTTPX Client；设置 240 秒超时；初始化不发请求，也不把账号 Token 当模型 Key。
    def __init__(self, url=None):
        # CLI/MCP 会携带账号 Token，先核实平台目标确为 HTTPS；证书验证由 HTTPX 和 SSL_CERT_FILE 的受信 CA 合集完成。
        base_url = require_https_url(url or os.environ.get("TESTPILOT_URL", "https://127.0.0.1:8020"), "平台 SDK 地址")
        token = os.environ.get("TESTPILOT_ACCESS_TOKEN", "")
        self.http = httpx.Client(base_url=base_url, headers=dict({"Authorization": "Bearer " + token} if token else {}, **({"X-TestPilot-Project":os.environ["TESTPILOT_PROJECT_ID"]} if os.environ.get("TESTPILOT_PROJECT_ID") else {})), timeout=240)

    # 功能：发送平台 HTTP 请求并把成功响应解码为 JSON，失败 HTTP 状态交调用方处理。
    # 输入：method/path 是 GET/POST 等方法和 API 路径；kwargs 是 HTTPX 支持的 json、params、content、headers 等请求参数。
    # 输出/影响：返回响应 JSON 对象；会访问平台网络，非成功状态抛 HTTPStatusError；不持久保存请求/响应或自动执行模型循环。
    def request(self, method, path, **kwargs):
        # path 应指向已配置平台，不能用绝对 HTTP URL 或其他主机覆盖 base_url 后顺带发送账号 Token。
        target = self.http.base_url.join(path)
        if target.scheme != "https" or target.host != self.http.base_url.host or target.port != self.http.base_url.port or target.username or target.password:
            raise ValueError("平台 SDK 请求必须使用配置的 HTTPS 平台地址")
        response = self.http.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    # 将工具名和参数封装成平台 HTTP 请求，业务实现仍在 tools.dispatch/testing 中。
    # 功能：把统一工具名及参数包装成 /api/tools 请求，复用平台已有业务实现。
    # 输入：name 是工具名；arguments 是参数字典；task_id 可关联 Agent 对话；key 是重复提交防重用的幂等标识。
    # 输出/影响：返回平台 JSON 工具结果；会发 POST，请求的实际保存/执行副作用由服务端业务及绑定执行器决定，SDK 不另写引擎。
    def tool(self, name, arguments, task_id="", key=""):
        return self.request("POST", "/api/tools/"+name, json={"arguments": arguments, "task_id": task_id, "key": key})

    # 功能：关闭本 SDK 实例的 HTTPX 连接池，避免 CLI 或 MCP 临时客户端泄漏连接。
    # 输入：self.http 是初始化时创建的 HTTPX Client；无需额外参数。
    # 输出/影响：返回 None；释放本客户端网络资源，不停止已排队的测试或 Agent 任务，也不删账号/项目记录。
    def close(self):
        self.http.close()
