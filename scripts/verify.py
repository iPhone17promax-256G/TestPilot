"""Repeatable live smoke checks; --execute creates real run records, without LLM calls."""
import argparse
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument("--execute", action="store_true")
args = parser.parse_args()
base = os.environ.get("AIQA_URL", "http://127.0.0.1:8020").rstrip("/")
token = os.environ.get("AIQA_ACCESS_TOKEN", "")
if not token:
    env_file=Path(__file__).resolve().parents[1]/".env"
    if env_file.exists():token=next((line.split("=",1)[1] for line in env_file.read_text().splitlines() if line.startswith("AIQA_ACCESS_TOKEN=")),"")

def request(path, body=None):
    headers = {"Content-Type": "application/json", "X-AIQA-Project":os.environ.get("AIQA_PROJECT_ID","PROJECT")}
    if token:
        headers["Authorization"] = "Bearer "+token
    req = Request(base+"/api"+path, data=None if body is None else json.dumps(body).encode(), headers=headers)
    with urlopen(req, timeout=100) as response:
        return json.load(response)

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
        ("导入错误", "api", "import aiqa_nonexistent_test_dependency\n\ndef test_check():\n    assert True\n", "error"),
        ("动态等待", "web", 'from playwright.sync_api import expect\n\ndef test_delayed(page):\n    page.set_content("<div id=target></div><script>setTimeout(()=>document.getElementById(\'target\').innerHTML=\'<button>Ready</button>\',750)</script>")\n    expect(page.get_by_role("button",name="Ready")).to_be_visible()\n', "passed"),
    ]
    for name, kind, code, expected in checks:
        case = tool("cases_save_revision", {"name": "自检 · "+name, "kind": kind, "code": code}, key="smoke-case-v1-"+name)
        run = request("/runs", {"case_id": case["id"], "revision_number": 1, "environment_id": state["environments"][0]["id"]})
        report = tool("runs_get", {"id": run["id"], "wait_seconds": 90})
        assert report["status"] == expected, report
        print(json.dumps({"check": name, "run": run["id"], "status": report["status"]}, ensure_ascii=False))
print("AIQA checks passed.")
