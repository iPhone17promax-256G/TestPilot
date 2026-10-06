# 检测实际本地执行器与测试镜像的能力，不检测浏览器页面里的 JavaScript 环境。
# 仅检查并给安装指引；不会替用户安装依赖，也不会调用模型产生测试费用。

"""Read-only capability checks of the actual local executor and runner images."""
import importlib.metadata
import platform
import shutil
import time
from pathlib import Path
from .config import settings

DOCS={
 "core":("https://docs.docker.com/engine/install/","docker version"),
 "api":("https://www.python-httpx.org/","python -m pip install -e ."),
 "web":("https://playwright.dev/python/docs/docker","docker build -t aiqa-runtime:0.1 -f deploy/Dockerfile ."),
 "browser":("https://docs.browser-use.com/open-source/quickstart","python -m playwright install chromium"),
 "performance":("https://jmeter.apache.org/usermanual/get-started.html","docker build -t aiqa-jmeter:0.1 -f deploy/jmeter/Dockerfile ."),
 "app":("https://appium.io/docs/en/latest/quickstart/uiauto2-driver/","appium driver install uiautomator2")}


# 默认检查包/镜像/磁盘等存在性；smoke=True 才启动无网络容器验证 Chromium 真能打开。
# ready 的含义要看 detail：镜像存在不等于所有复杂用例能通过；APP 设备还需单独联机检查。
def probe(smoke=False):
    rows=[]
    def add(identity,label,status,detail,where="本地执行器"):
        url,command=DOCS[identity];rows.append(dict(id=identity,label=label,status=status,detail=detail,location=where,docs=url,command=command))
        if settings.managed_executor:
            row=rows[-1]
            row["location"]=where.replace("本地", "服务器") if settings.deployment_mode=="server" else where
            row["command"]="docker image inspect "+({"performance":settings.performance_image,"app":settings.appium_image}.get(identity,settings.runner_image))
    settings.client_root.mkdir(parents=True,exist_ok=True)
    free=shutil.disk_usage(settings.client_root).free
    add("core","Python 与工作目录","ready" if free>512*1024*1024 else "error",f"Python {platform.python_version()}；可用磁盘 {free//1024//1024} MB")
    def installed(package):
        try:return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:return "未安装"
    add("api","接口测试","ready" if installed("httpx")!="未安装" else "missing","HTTPX "+installed("httpx"))
    add("browser","AI 浏览器探索","ready" if installed("browser-use")!="未安装" and Path("/usr/lib/chromium/chromium").exists() else "missing","browser-use "+installed("browser-use")+"；系统 Chromium "+("已安装" if Path("/usr/lib/chromium/chromium").exists() else "未找到"))
    client=None
    try:
        import docker
        client=docker.from_env();client.ping()
        add("core","Docker 执行隔离","ready","Docker Engine "+client.version().get("Version","未知"))
        image=client.images.get(settings.runner_image)
        detail="运行镜像 "+settings.runner_image+"（"+image.id[:19]+"）"
        if smoke:
            code="from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True,executable_path='/usr/lib/chromium/chromium',args=['--no-sandbox']); page=b.new_page(); page.set_content('<h1>AIQA</h1>'); assert page.locator('h1').inner_text()=='AIQA'; b.close(); p.stop(); print('Chromium launch passed')"
            container=client.containers.run(settings.runner_image,["python","-c",code],detach=True,network_mode="none",mem_limit="768m",pids_limit=256,labels={"aiqa.environment-probe":"true"})
            try:
                result=container.wait(timeout=35)
                if result["StatusCode"]!=0:raise RuntimeError("浏览器启动自检失败")
                detail+="；无网络浏览器启动自检通过"
            finally:container.remove(force=True)
        add("web","Web UI 测试","ready",detail,"隔离测试容器")
    except Exception:
        add("web","Web UI 测试","missing","Docker 不可用、运行镜像缺失或浏览器启动失败；按安装指引检查","隔离测试容器")
    try:
        if not client:raise RuntimeError()
        client.images.get(settings.performance_image)
        add("performance","JMeter 性能测试","ready","JMeter 镜像已安装；负载在本地容器执行","隔离性能容器")
    except Exception:add("performance","JMeter 性能测试","missing","未找到性能运行镜像","隔离性能容器")
    # Appium server/driver/device must be checked together from the device page.
    add("app","APP 测试","optional","Appium Python 客户端 "+installed("Appium-Python-Client")+"；请在设备页面检查 Appium 服务、UiAutomator2 驱动及 Android 连接","本地 Appium 服务与设备")
    if client:client.close()
    return dict(checkedAt=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),os=platform.system(),role="server-executor" if settings.deployment_mode=="server" and settings.managed_executor else "local-executor",checks=rows,smoke=smoke)
