# Android/Appium 集成：登记设备和应用、管理独占租约，以及给 Agent 提供即时观察/操作。
# 正式 APP 用例执行另走 pytest driver fixture；探索会话与回归 Run 是不同对象。

"""Android application assets, exclusive leases and WebDriver exploration."""
import base64, hashlib, json, time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse
import httpx
from sqlalchemy import select, text
from .storage import Record, DeviceLease, Session, engine, list_records, now, read_record, session, uid, update_record
from .testing import safe_path, stamp

def http_url(value):
    p=urlparse(value)
    if p.scheme not in {"http","https"} or not p.hostname or p.username or p.password:
        raise ValueError("节点地址必须是无账号密码的 HTTP/HTTPS URL")
    return value.rstrip("/")

def device_save(name,udid,appium_url,node_url="",device_id=""):
    if not name.strip() or not udid.strip():raise ValueError("设备名称和 udid 不能为空")
    url=http_url(appium_url)
    # Serialize registration so a physical device cannot acquire two identities.
    with engine.connect() as connection:
        if connection.scalar(text("SELECT GET_LOCK('aiqa_devices', 15)"))!=1:raise ValueError("设备配置正在更新，请重试")
        connection.commit()
        try:
            with Session(bind=connection) as db, db.begin():
                devices=list(db.scalars(select(Record).where(Record.kind=="device")))
                duplicate=next((d for d in devices if d.payload["appiumUrl"]==url and d.payload["udid"]==udid.strip() and d.id!=device_id),None)
                if duplicate:raise ValueError("这个 Appium 节点上的设备已登记："+duplicate.id)
                row=db.get(Record,device_id) if device_id else None
                if device_id and (not row or row.kind!="device"):raise ValueError("设备 ID 不正确")
                if row:
                    lease=db.get(DeviceLease,device_id)
                    if lease and lease.expires_at>now():raise ValueError("设备正在使用，不能修改连接配置")
                used={d.payload.get("systemPort",8200) for d in devices if d.id!=device_id and d.payload["appiumUrl"]==url}
                port=(row.payload.get("systemPort",8200) if row else next((n for n in range(8200,8300) if n not in used),None))
                if port is None or port in used:raise ValueError("Appium 节点的设备端口已用完，请使用另一个节点")
                payload=dict(id=device_id or uid("DEVICE"),name=name.strip(),udid=udid.strip(),appiumUrl=url,nodeUrl=http_url(node_url) if node_url else "",systemPort=port,platform="android",status="unknown",createdAt=row.payload["createdAt"] if row else stamp())
                if row:row.payload=payload
                else:db.add(Record(id=payload["id"],kind="device",payload=payload))
        finally:
            connection.execute(text("SELECT RELEASE_LOCK('aiqa_devices')"));connection.commit()
    return payload

def devices_list():
    values=list_records("device")
    with session() as db:
        for d in values:
            lease=db.get(DeviceLease,d["id"])
            d["busy"]=bool(lease and lease.expires_at>now())
            d["owner"]=lease.owner if d["busy"] else ""
    return values

# 真实请求 Appium 并检查设备能力；保存一个 IP/端口并不证明设备可用。
def device_check(device_id):
    device=read_record(device_id,"device")
    try:
        r=httpx.get(device["appiumUrl"]+"/status",timeout=8);r.raise_for_status()
        available=r.json().get("value",{}).get("ready",False)
        if device["nodeUrl"]:
            r=httpx.get(device["nodeUrl"]+"/health",timeout=8);r.raise_for_status()
            entry=next((x for x in r.json()["devices"] if x["udid"]==device["udid"]),None)
            available=available and bool(entry and entry["state"]=="device")
            if entry:device["androidVersion"]=entry["androidVersion"]
        device.update(status="online" if available else "offline",lastCheck=stamp(),message="设备与 Appium 可用" if available else "设备未在线或 Appium 未就绪")
    except (httpx.HTTPError,ValueError,KeyError):
        device.update(status="offline",lastCheck=stamp(),message="无法连接设备节点或 Appium")
    update_record(device_id,lambda p:p.update(device))
    return next(d for d in devices_list() if d["id"]==device_id)

def app_save(name,package,activity="",version="installed"):
    if not name.strip() or not package.strip():raise ValueError("应用名称和包名不能为空")
    payload=dict(id=uid("APP"),name=name.strip(),package=package.strip(),activity=activity.strip(),version=version,platform="android",path="",hash=hashlib.sha256(json.dumps([package,activity,version]).encode()).hexdigest(),createdAt=stamp(),filename="已安装应用")
    with session() as db:db.add(Record(id=payload["id"],kind="application",payload=payload))
    return payload

# 保存 APK 原文件并计算内容摘要；这是待安装测试资产，不是在上传接口里安装应用。
def app_upload(data,name,package="",activity="",version=""):
    import zipfile,io
    if not data or len(data)>100_000_000:raise ValueError("APK 不能为空，且不能超过 100MB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as apk:
            if "AndroidManifest.xml" not in apk.namelist():raise ValueError("文件不是有效 APK：缺少 AndroidManifest.xml")
    except zipfile.BadZipFile as e:raise ValueError("文件不是有效 APK ZIP") from e
    digest=hashlib.sha256(data).hexdigest()
    relative=f"apps/{digest}.apk";path=safe_path(relative);path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():path.write_bytes(data)
    payload=dict(id=uid("APP"),name=name or "Android APK",package=package,activity=activity,version=version or digest[:12],platform="android",path=relative,hash=digest,createdAt=stamp(),filename=(name or "application")+".apk")
    with session() as db:db.add(Record(id=payload["id"],kind="application",payload=payload))
    return payload

def stop_session(url,identity):
    if identity:
        try:httpx.delete(url+"/session/"+identity,timeout=12)
        except httpx.HTTPError:pass

# 取得设备独占租约后才能操作。当前 executor 通过 HTTP 请求平台仲裁，不只锁本机缓存。
# 前一次租约过期遗留的 Appium 会话需要清理，否则新任务可能遇到旧会话干扰。
def acquire(device_id,owner):
    from .config import settings
    if settings.role=="executor":
        from .replica import request
        result=request("POST","/api/executors/device-lease",json=dict(device_id=device_id,owner=owner,operation="acquire")).json()
        previous=result.get("previous",{})
        if previous.get("session"):stop_session(previous["url"],previous["session"])
        return result["device"]
    # Lock the stable device record first, including the first ever lease.
    with session() as db:
        device=db.scalar(select(Record).where(Record.id==device_id,Record.kind=="device").with_for_update())
        if not device:raise ValueError("设备不存在")
        lease=db.get(DeviceLease,device_id)
        if lease and lease.owner!=owner and lease.expires_at>now():raise ValueError("设备已被任务占用："+lease.owner)
        if lease and lease.owner!=owner and lease.session_id:
            stop_session(lease.appium_url,lease.session_id)
        if not lease:
            lease=DeviceLease(device_id=device_id,owner=owner,expires_at=now());db.add(lease)
        lease.owner=owner;lease.expires_at=now()+timedelta(seconds=90);lease.session_id="";lease.appium_url=device.payload["appiumUrl"]
        return dict(device.payload)

def renew(device_id,owner,session_id=""):
    from .config import settings
    if settings.role=="executor":
        from .replica import request
        request("POST","/api/executors/device-lease",json=dict(device_id=device_id,owner=owner,operation="renew",session_id=session_id))
        return
    with session() as db:
        lease=db.scalar(select(DeviceLease).where(DeviceLease.device_id==device_id).with_for_update())
        if not lease or lease.owner!=owner:raise RuntimeError("设备租约已失效")
        lease.expires_at=now()+timedelta(seconds=90)
        if session_id:lease.session_id=session_id

def release(device_id,owner):
    from .config import settings
    if settings.role=="executor":
        from .replica import request
        result=request("POST","/api/executors/device-lease",json=dict(device_id=device_id,owner=owner,operation="release")).json()
        stop_session(result.get("url",""),result.get("session",""))
        return
    with session() as db:
        lease=db.scalar(select(DeviceLease).where(DeviceLease.device_id==device_id).with_for_update())
        if lease and lease.owner==owner:
            stop_session(lease.appium_url,lease.session_id)
            db.delete(lease)

def cleanup_expired():
    with session() as db:
        leases=list(db.scalars(select(DeviceLease).where(DeviceLease.expires_at<=now()).with_for_update(skip_locked=True)))
        for lease in leases:
            stop_session(lease.appium_url,lease.session_id);db.delete(lease)

# 组装 Appium 会话参数：Android、UiAutomator2、设备、应用及重置方式。
# APK 先核对摘要，再放入 Appium 专用共享目录；Appium 不需要读取模型密钥或检查点目录。
def capabilities(app,device,reset="reset"):
    caps={"platformName":"Android","appium:automationName":"UiAutomator2","appium:udid":device["udid"],"appium:deviceName":device["name"],"appium:newCommandTimeout":75,"appium:systemPort":device.get("systemPort",8200),"appium:noReset":reset=="retain","appium:forceAppLaunch":True,"appium:shouldTerminateApp":True,"appium:autoGrantPermissions":False}
    if app["path"]:
        from .config import settings
        if settings.role=="executor":
            import hashlib, os, tempfile
            content=safe_path(app["path"]).read_bytes()
            digest=hashlib.sha256(content).hexdigest()
            if app.get("hash") and app["hash"]!=digest:raise ValueError("APK 内容校验失败")
            # Appium sees only APKs, never the client vault or checkpoints.
            folder=settings.client_root/"apks";folder.mkdir(parents=True,exist_ok=True)
            shared=folder/(digest+".apk")
            if shared.is_symlink():raise ValueError("APK 缓存不允许符号链接")
            if not shared.exists() or hashlib.sha256(shared.read_bytes()).hexdigest()!=digest:
                fd,name=tempfile.mkstemp(dir=folder,suffix=".apk")
                try:
                    with os.fdopen(fd,"wb") as handle:handle.write(content)
                    os.chmod(name,0o644);os.replace(name,shared)
                finally:
                    if os.path.exists(name):os.unlink(name)
            caps["appium:app"]=str(shared)
        else:caps["appium:app"]="/apps/"+Path(app["path"]).name
    if app.get("package"):caps["appium:appPackage"]=app["package"]
    if app.get("activity"):caps["appium:appActivity"]=app["activity"]
    return caps

# 通过 HTTP 调 Appium 的 WebDriver 协议；Appium 再把动作交给 Android 自动化驱动执行。
def webdriver(device,method,path,body=None):
    with httpx.Client(timeout=70) as client:
        r=client.request(method,device["appiumUrl"]+path,json=body)
        value=r.json().get("value",{})
        if r.is_error or isinstance(value,dict) and value.get("error"):
            raise ValueError("Appium 操作失败："+str(value.get("message",value))[:1000])
        return value

# Agent 的 open/observe/click 等即时工具，返回会话 ID 后才能继续操作同一设备会话。
# 校验 Task 归属、续租并保存观察证据；结束应 close，正式测试需另外保存用例和启动 Run。
def mobile_action(action,device_id="",app_id="",session_id="",selector="",by="accessibility id",text="",direction="up",reset="reset",task_id=""):
    if action=="open":
        device=read_record(device_id,"device");app=read_record(app_id,"application")
        identity=uid("MOBILE");inspection=uid("INS");owner=identity
        device=acquire(device_id,owner)
        try:
            value=webdriver(device,"POST","/session",{"capabilities":{"alwaysMatch":capabilities(app,device,reset),"firstMatch":[{}]}})
            native=value["sessionId"];renew(device_id,owner,native)
            payload=dict(id=identity,deviceId=device_id,appId=app_id,nativeId=native,taskId=task_id,device=device,inspectionId=inspection,status="active")
            with session() as db:db.add(Record(id=identity,kind="mobile_session",status="active",payload=payload))
            if task_id:
                update_record(task_id,lambda p:p.update(mobileSessionIds=[*p.get("mobileSessionIds",[]),identity]))
            return dict(session_id=identity,device=device["name"],app=app["name"],message="设备已独占；调用 observe 获取截图和元素，结束后调用 close")
        except Exception:
            release(device_id,owner);raise
    item=read_record(session_id,"mobile_session")
    if task_id and item.get("taskId")!=task_id:raise ValueError("会话不属于当前 Agent 任务")
    device=item["device"];prefix="/session/"+item["nativeId"]
    if action=="close":
        release(item["deviceId"],item["id"])
        update_record(item["id"],lambda p:p.update(status="closed"))
        return {"session_id":session_id,"status":"closed"}
    if item["status"]!="active":raise ValueError("设备会话已关闭，请重新打开应用")
    try:
        renew(item["deviceId"],item["id"],item["nativeId"])
    except RuntimeError as error:
        update_record(item["id"],lambda p:p.update(status="expired"))
        raise ValueError("设备会话已超时，请重新打开应用") from error
    if action in {"click","input"}:
        if by not in {"accessibility id","id","xpath","-android uiautomator"} or not selector:raise ValueError("需要支持的定位方式和 selector")
        deadline=time.monotonic()+10
        while True:
            elements=webdriver(device,"POST",prefix+"/elements",{"using":by,"value":selector})
            if elements:break
            if time.monotonic()>=deadline:raise ValueError("等待元素超时："+selector)
            time.sleep(.4)
        element=elements[0].get("element-6066-11e4-a52e-4f735466cecf") or elements[0].get("ELEMENT")
        if action=="click":webdriver(device,"POST",prefix+"/element/"+element+"/click",{})
        else:
            webdriver(device,"POST",prefix+"/element/"+element+"/clear",{})
            webdriver(device,"POST",prefix+"/element/"+element+"/value",{"text":text,"value":list(text)})
    elif action=="back":webdriver(device,"POST",prefix+"/back",{})
    elif action=="swipe":
        rectangle=webdriver(device,"GET",prefix+"/window/rect")
        webdriver(device,"POST",prefix+"/execute/sync",{"script":"mobile: swipeGesture","args":[dict(left=20,top=100,width=max(10,rectangle["width"]-40),height=max(10,rectangle["height"]-200),direction=direction,percent=.7)]})
    elif action!="observe":raise ValueError("未知设备操作")
    source=webdriver(device,"GET",prefix+"/source")
    png=webdriver(device,"GET",prefix+"/screenshot")
    relative=f"inspections/{item['inspectionId']}";folder=safe_path(relative);folder.mkdir(parents=True,exist_ok=True)
    image_path=f"{relative}/{uid('SCREEN')}.png";safe_path(image_path,fetch=False).write_bytes(base64.b64decode(png))
    safe_path(relative+"/page.xml",fetch=False).write_text(source)
    if task_id:
        def observed(p):
            inspections=list(p.get("inspections",[]))
            existing=next((x for x in inspections if x["id"]==item["inspectionId"]),None)
            value=dict(id=item["inspectionId"],status="completed",summary="Android 应用观察："+app_name(item["appId"]),urls=[],actions=[action],artifacts=[dict(name="APP 截图",url="/api/artifacts/"+image_path),dict(name="page.xml",url="/api/artifacts/"+relative+"/page.xml")])
            if existing:
                value["actions"]=[*existing["actions"],action];inspections[inspections.index(existing)]=value
            else:inspections.append(value)
            p["inspections"]=inspections
        update_record(task_id,observed)
    import xml.etree.ElementTree as ET
    root=ET.fromstring(source); compact=ET.Element("screen")
    for node in root.iter():
        if node.get("text") or node.get("content-desc") or node.get("clickable")=="true" or node.get("scrollable")=="true":
            attrs={k:v for k,v in node.attrib.items() if k in {"class","text","content-desc","resource-id","clickable","enabled","checked","checkable","scrollable","password","bounds","displayed"}}
            ET.SubElement(compact,"element",attrs)
    reduced=ET.tostring(compact,encoding="unicode")
    return dict(session_id=session_id,action=action,source=reduced[:30000],image=png,mimeType="image/png",path=image_path,sourcePath=relative+"/page.xml",sourceTruncated=len(reduced)>30000)

def app_name(identity):return read_record(identity,"application")["name"]

def close_task_sessions(task_id):
    for item in list_records("mobile_session"):
        if item.get("taskId")==task_id and item["status"]=="active":
            mobile_action("close",session_id=item["id"],task_id=task_id)
