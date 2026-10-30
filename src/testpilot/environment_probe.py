# 检测当前执行服务所在机器的 Python 依赖和 Docker 测试镜像；本地模式检查用户机器，服务端模式检查内网执行服务，不是检测网页 JavaScript 或用户随意上传的代码。
# 本模块的 probe 只检查环境并给缺失项指引，不安装依赖、不调用模型；平台另有固定测试组件的安装入口，不能因此说整个平台都没有安装能力。

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
 "web":("https://playwright.dev/python/docs/docker","docker build -t testpilot-runtime:0.1 -f deploy/Dockerfile ."),
 "browser":("https://docs.browser-use.com/open-source/quickstart","python -m playwright install chromium"),
 "performance":("https://jmeter.apache.org/usermanual/get-started.html","docker build -t testpilot-jmeter:0.1 -f deploy/jmeter/Dockerfile ."),
 "app":("https://appium.io/docs/en/latest/quickstart/uiauto2-driver/","appium driver install uiautomator2")}


# 默认检查包/镜像/磁盘等存在性；smoke=True 才启动无网络容器验证 Chromium 真能打开。
# ready 的含义要看 detail：镜像存在不等于所有复杂用例能通过；APP 设备还需单独联机检查。
# 功能：检查执行服务及各测试类型的依赖状态。
# 输入：smoke=False 只做常规检查，True 会实际启动一次隔离 Chromium 冒烟容器。
# 输出/影响：返回带时间/位置/安装指引的检查报告；读取包版本/Docker/磁盘，可能建目录和临时容器，不自动安装依赖。
def probe(smoke=False):
    rows=[]
    # 功能：向当前检查报告追加统一格式的检查项。
    # 输入：identity 对应 DOCS 中的组件，label/status/detail 描述结果，where 说明执行位置；rows 来自外层闭包。
    # 输出/影响：返回 None；修改内存 rows，受管部署调整位置文字和镜像检查命令，不执行所显示的安装命令。
    def add(identity,label,status,detail,where="本地执行器"):
        url,command=DOCS[identity];rows.append(dict(id=identity,label=label,status=status,detail=detail,location=where,docs=url,command=command))
        if settings.managed_executor:
            row=rows[-1]
            row["location"]=where.replace("本地", "服务器") if settings.deployment_mode=="server" else where
            row["command"]="docker image inspect "+({"performance":settings.performance_image,"app":settings.appium_image}.get(identity,settings.runner_image))
    settings.client_root.mkdir(parents=True,exist_ok=True)
    free=shutil.disk_usage(settings.client_root).free
    add("core","Python 与工作目录","ready" if free>512*1024*1024 else "error",f"Python {platform.python_version()}；可用磁盘 {free//1024//1024} MB")
    # 功能：读取某个 Python 分发包的已安装版本。
    # 输入：package 是 importlib.metadata 使用的分发包名称。
    # 输出/影响：返回版本文字或“未安装”；只读包元信息，不下载或导入整个组件。
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
            code="from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True,executable_path='/usr/lib/chromium/chromium',args=['--no-sandbox']); page=b.new_page(); page.set_content('<h1>TestPilot</h1>'); assert page.locator('h1').inner_text()=='TestPilot'; b.close(); p.stop(); print('Chromium launch passed')"
            container=client.containers.run(settings.runner_image,["python","-c",code],detach=True,network_mode="none",mem_limit="768m",pids_limit=256,labels={"testpilot.environment-probe":"true"})
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
    # Python Appium 包存在不代表手机可用；还需要在设备页实际检查 Appium 服务、UiAutomator2 驱动和 Android 设备连接。
    add("app","APP 测试","optional","Appium Python 客户端 "+installed("Appium-Python-Client")+"；请在设备页面检查 Appium 服务、UiAutomator2 驱动及 Android 连接","本地 Appium 服务与设备")
    if client:client.close()
    return dict(checkedAt=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),os=platform.system(),role="server-executor" if settings.deployment_mode=="server" and settings.managed_executor else "local-executor",checks=rows,smoke=smoke)
