"""Register demo metadata; device checks require a paired user's local executor."""
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("AIQA_URL", "http://127.0.0.1:8020").rstrip("/")
token = os.environ.get("AIQA_ACCESS_TOKEN", "")
if not token and (ROOT/".env").exists():
    for line in (ROOT/".env").read_text().splitlines():
        if line.startswith("AIQA_ACCESS_TOKEN="):
            token=line.split("=",1)[1].strip().strip('"').strip("'")

def request(path, body=None, raw=None):
    headers = {"Authorization":"Bearer "+token} if token else {}
    data = json.dumps(body).encode() if body is not None else raw
    if body is not None: headers["Content-Type"]="application/json"
    if raw is not None: headers["Content-Type"]="application/octet-stream"
    req=urllib.request.Request(BASE+"/api"+path,data=data,headers=headers)
    try:
        return json.load(urllib.request.urlopen(req,timeout=20))
    except urllib.error.HTTPError as error:
        raise RuntimeError(error.read().decode()) from error

state=request("/state")
device=next((d for d in state["devices"] if d["udid"]=="emulator-5554" and d["appiumUrl"]=="http://appium:4723"),None)
if not device:
    device=request("/devices",dict(name="AIQA Android 模拟器",udid="emulator-5554",appium_url="http://appium:4723",node_url="http://appium:4724"))
app=next((a for a in state["applications"] if a["name"]=="ApiDemos" and a["version"]=="6.0.18" and a.get("path")),None)
if not app:
    path=ROOT/".runtime/ApiDemos.apk"
    subprocess.run(["docker","cp","aiqa-appium-1:/opt/ApiDemos.apk",str(path)],check=True,capture_output=True)
    query=urllib.parse.urlencode(dict(name="ApiDemos",package="io.appium.android.apis",activity=".ApiDemos",version="6.0.18"))
    app=request("/applications/upload?"+query,raw=path.read_bytes())
print(json.dumps(dict(device_id=device["id"],app_id=app["id"],status="registered"),ensure_ascii=False))
print("Demo registered. Pair your local executor, then check the device on /mobile. First emulator boot may take several minutes.")
