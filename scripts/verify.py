"""Repeatable live smoke checks; --execute creates real run records, without LLM calls."""
import argparse
import json
import os
from pathlib import Path
from urllib.request import Request
from https_client import require_https_url,open_https

parser = argparse.ArgumentParser()
parser.add_argument("--execute", action="store_true")
args = parser.parse_args()
base = require_https_url(os.environ.get("TESTPILOT_URL", "https://127.0.0.1:8020"))
token = os.environ.get("TESTPILOT_ACCESS_TOKEN", "")
if not token:
    env_file=Path(__file__).resolve().parents[1]/".env"
    if env_file.exists():token=next((line.split("=",1)[1] for line in env_file.read_text().splitlines() if line.startswith("TESTPILOT_ACCESS_TOKEN=")),"")

# 功能：向已启动的平台发送冒烟验收请求。
# 输入：path 是 API 子路径，body 是可选 JSON；平台地址、访问 token、项目 ID 从脚本环境读取。
# 输出/影响：返回 JSON；无 body 为读取请求，有 body 可修改验收业务数据，函数本身不调用 LLM。
def request(path, body=None):
    headers = {"Content-Type": "application/json", "X-TestPilot-Project":os.environ.get("TESTPILOT_PROJECT_ID","PROJECT")}
    if token:
        headers["Authorization"] = "Bearer "+token
    req = Request(base+"/api"+path, data=None if body is None else json.dumps(body).encode(), headers=headers)
    with open_https(req, timeout=100) as response:
        return json.load(response)

# 功能：通过通用 HTTP 工具入口调用平台能力。
# 输入：name 是工具名，arguments 是工具参数字典，key 是可选幂等键。
# 输出/影响：返回 request 的工具结果；保存/执行类工具会创建资产或测试作业，查询类只读。
def tool(name, arguments, key=""):
    return request("/tools/"+name, {"arguments": arguments, "key": key})

health = request("/health")
assert health == {"status": "ok", "database": "mysql", "mode": "live"}, health
state = request("/state")
assert state["environments"], "No target environment configured"
print(json.dumps({"health": health, "cases": len(state["cases"]), "runs": len(state["runs"]), "tasks": len(state["tasks"])}, ensure_ascii=False))
if args.execute:
    checks = [
        ("通过", "api", "def test_check():\n    assert 2 + 2 == 4\n", "passed"),
        ("断言失败", "api", "def test_check():\n    assert 2 + 2 == 5\n", "failed"),
        ("零测试", "api", "VALUE = 1\n", "error"),
        ("全部跳过", "api", 'import pytest\n\ndef test_check():\n    pytest.skip("not exercised")\n', "error"),
        ("导入错误", "api", "import testpilot_nonexistent_test_dependency\n\ndef test_check():\n    assert True\n", "error"),
        ("动态等待", "web", 'from playwright.sync_api import expect\n\ndef test_delayed(page):\n    page.set_content("<div id=target></div><script>setTimeout(()=>document.getElementById(\'target\').innerHTML=\'<button>Ready</button>\',750)</script>")\n    expect(page.get_by_role("button",name="Ready")).to_be_visible()\n', "passed"),
    ]
    for name, kind, code, expected in checks:
        case = tool("cases_save_revision", {"name": "自检 · "+name, "kind": kind, "code": code}, key="smoke-case-v1-"+name)
        run = request("/runs", {"case_id": case["id"], "revision_number": 1, "environment_id": state["environments"][0]["id"]})
        report = tool("runs_get", {"id": run["id"], "wait_seconds": 90})
        assert report["status"] == expected, report
        print(json.dumps({"check": name, "run": run["id"], "status": report["status"]}, ensure_ascii=False))
print("TestPilot checks passed.")
