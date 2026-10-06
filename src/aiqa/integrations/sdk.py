# 外部调用的轻量 HTTP 客户端，CLI 和独立 stdio MCP 复用它。
# 账号 Token 证明调用者身份，X-AIQA-Project 选择项目；它们不是模型供应商的 Key。

import os
import httpx


class Client:
    def __init__(self, url=None):
        token = os.environ.get("AIQA_ACCESS_TOKEN", "")
        self.http = httpx.Client(base_url=url or os.environ.get("AIQA_URL", "http://127.0.0.1:8020"), headers=dict({"Authorization": "Bearer " + token} if token else {}, **({"X-AIQA-Project":os.environ["AIQA_PROJECT_ID"]} if os.environ.get("AIQA_PROJECT_ID") else {})), timeout=240)

    def request(self, method, path, **kwargs):
        response = self.http.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    # 将工具名和参数封装成平台 HTTP 请求，业务实现仍在 tools.dispatch/testing 中。
    def tool(self, name, arguments, task_id="", key=""):
        return self.request("POST", "/api/tools/"+name, json={"arguments": arguments, "task_id": task_id, "key": key})

    def close(self):
        self.http.close()
