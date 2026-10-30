"""Register demo metadata; device checks require a paired user's local executor."""
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from https_client import require_https_url,open_https

ROOT = Path(__file__).resolve().parents[1]
BASE = require_https_url(os.environ.get("TESTPILOT_URL", "https://127.0.0.1:8020"))
token = os.environ.get("TESTPILOT_ACCESS_TOKEN", "")
if not token and (ROOT/".env").exists():
    for line in (ROOT/".env").read_text().splitlines():
        if line.startswith("TESTPILOT_ACCESS_TOKEN="):
            token=line.split("=",1)[1].strip().strip('"').strip("'")

# 功能：向平台提交 Android 演示资源的登记请求。
# 输入：path 是 /api 下的子路径，body 是可选 JSON，raw 是可选二进制内容；BASE/token 来自脚本环境。
# 输出/影响：返回平台 JSON；body/raw 会让请求携带数据，HTTP 错误转异常，函数本身不执行 adb 或安装手机应用。
def request(path, body=None, raw=None):
    headers = {"Authorization":"Bearer "+token} if token else {}
    data = json.dumps(body).encode() if body is not None else raw
    if body is not None: headers["Content-Type"]="application/json"
    if raw is not None: headers["Content-Type"]="application/octet-stream"
    req=urllib.request.Request(BASE+"/api"+path,data=data,headers=headers)
    try:
        return json.load(open_https(req,timeout=20))
    except urllib.error.HTTPError as error:
        raise RuntimeError(error.read().decode()) from error

state=request("/state")
device=next((d for d in state["devices"] if d["udid"]=="emulator-5554" and d["appiumUrl"]=="http://appium:4723"),None)
if not device:
    device=request("/devices",dict(name="TestPilot Android 模拟器",udid="emulator-5554",appium_url="http://appium:4723",node_url="http://appium:4724"))
app=next((a for a in state["applications"] if a["name"]=="ApiDemos" and a["version"]=="6.0.18" and a.get("path")),None)
if not app:
    path=ROOT/".runtime/ApiDemos.apk"
    subprocess.run(["docker","cp","testpilot-appium-1:/opt/ApiDemos.apk",str(path)],check=True,capture_output=True)
    query=urllib.parse.urlencode(dict(name="ApiDemos",package="io.appium.android.apis",activity=".ApiDemos",version="6.0.18"))
    app=request("/applications/upload?"+query,raw=path.read_bytes())
print(json.dumps(dict(device_id=device["id"],app_id=app["id"],status="registered"),ensure_ascii=False))
print("Demo registered. Pair your local executor, then check the device on /mobile. First emulator boot may take several minutes.")
