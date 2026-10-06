---
name: mm-api-discovery
description: 使用外部 MCP 查 MM 的 ping 和登录接口示例，编写可视化接口用例并按要求执行。
---

按本轮用户要求行动；只要求编写时不要执行。

1. 先分别调用 MCP 的 mm_endpoint(name="ping")、mm_endpoint(name="login") 取得两条真实接口资料。不要把说明写成已经执行。
2. 读取 project_context，使用当前环境。
3. 优先保存可视化 API steps，用 request 发请求，再用 assert 检查 HTTP 状态及 JSON status。
4. 明确要求执行时，保存版本后 runs_start，再 runs_get 查询报告。
5. 附带 scripts/summarize.py 是可选的辅助程序。需要时可以读取资源、复制到工作区再 workspace_exec；不要求每次执行它。
