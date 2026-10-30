"""固定镜像组件管理。只接受枚举组件，后台线程返回状态；不执行浏览器提交的 shell 命令。"""
import threading
from .config import settings
from .storage import uid,now

_lock=threading.Lock()
_jobs={}


# 功能：列出平台允许操作的固定可选组件镜像。
# 输入：无参数；读取性能与 Appium 镜像配置。
# 输出/影响：返回 performance/app 到镜像名的字典，不访问 Docker。
def images():
    return {"performance":settings.performance_image,"app":settings.appium_image}


# 功能：检查 JMeter/Appium 镜像及近期组件作业。
# 输入：无参数；使用本机 Docker Engine 和 images 固定列表。
# 输出/影响：返回安装状态与内存中的最近作业；读取镜像并释放 Docker Client，不安装组件。
def list_components():
    import docker
    client=docker.from_env()
    try:
        rows=[]
        for identity,image in images().items():
            try:client.images.get(image);installed=True
            except docker.errors.ImageNotFound:installed=False
            rows.append(dict(id=identity,image=image,installed=installed))
        return dict(components=rows,jobs=list(_jobs.values())[-20:])
    finally:client.close()


# 功能：提交一个固定组件的后台安装或启动操作。
# 输入：component 是 performance/app，operation 是 install/start；JMeter 不接受单独 start。
# 输出/影响：创建内存作业、启动 _install 线程，返回作业副本；同一服务进程内只允许一个组件操作，历史最多保留 20 项，不提供跨进程锁。
def install(component,operation="install"):
    if component not in images() or operation not in {"install","start"}:raise ValueError("只支持固定的 JMeter / Appium 组件")
    if component=="performance" and operation=="start":raise ValueError("JMeter 由测试任务自动启动，安装镜像即可")
    with _lock:
        if any(j["status"]=="running" for j in _jobs.values()):raise ValueError("已有组件操作正在进行")
        identity=uid("INSTALL")
        job=dict(id=identity,component=component,operation=operation,status="running",logs=["开始处理固定组件 "+component],createdAt=now().isoformat())
        if len(_jobs)>=20:
            _jobs.pop(next(iter(_jobs)))
        _jobs[identity]=job
    threading.Thread(target=_install,args=(job,),daemon=True).start()
    return dict(job)


# 功能：在后台线程执行拉取镜像或启动 Appium 服务。
# 输入：job 是 install 创建的可修改作业字典，包含 component/operation。
# 输出/影响：返回 None；写内存日志/状态，可能下载镜像和创建/启动模拟器容器，异常转为作业 error，最终释放 Docker Client。
def _install(job):
    import docker
    from pathlib import Path
    client=None
    try:
        client=docker.from_env();image=images()[job["component"]]
        if job["operation"]=="install":
            try:client.images.get(image);job["logs"].append("镜像已存在，跳过下载")
            except docker.errors.ImageNotFound:
                job["logs"].append("下载安装配置中指定的镜像 "+image)
                client.images.pull(image)
                job["logs"].append("镜像下载完成")
        else:
            APPIUM_CONTAINER=settings.appium_container
            client.images.get(image)
            try:
                container=client.containers.get(APPIUM_CONTAINER)
                if container.labels.get("testpilot.component")!="app":raise ValueError("同名 Appium 容器不属于此安装，请用安装脚本启动或使用外部服务")
                container.start()
            except docker.errors.NotFound:
                if not Path("/dev/kvm").exists():raise ValueError("执行服务未挂载 /dev/kvm，首次启动请在安装目录运行 install.sh --android；也可接入外部 Appium")
                root=Path(__import__('os').environ.get("TESTPILOT_CLIENT_HOST_ROOT",str(settings.client_root)))
                client.containers.run(image,name=APPIUM_CONTAINER,detach=True,entrypoint="/opt/start-appium.sh",
                    network=settings.target_network,devices=["/dev/kvm:/dev/kvm"],
                    volumes={str(root/"apks"):{"bind":"/client/apks","mode":"ro"},"testpilot-managed-android":{"bind":"/root/.android/avd","mode":"rw"}},
                    labels={"testpilot.component":"app"},mem_limit="4g",nano_cpus=2_000_000_000,shm_size="512m",
                    restart_policy={"Name":"unless-stopped"})
            job["logs"].append("模拟器服务已启动，启动需要时间；请到应用与设备页检测连接")
        job["status"]="completed"
    except Exception as error:
        # Docker 错误可能包含仓库认证信息，不显示原始异常或请求对象。
        job["logs"].append(str(error) if isinstance(error,ValueError) else "组件操作失败：检查镜像仓库、网络和 Docker。离线镜像请用安装包 import-images.sh 导入。")
        job["status"]="error"
    finally:
        job["finishedAt"]=now().isoformat()
        if client:client.close()


# 功能：读取一份组件操作的当前快照。
# 输入：identity 是 INSTALL 作业 ID。
# 输出/影响：返回内存作业的复制字典；不存在时报错，服务重启后不会从数据库恢复这些日志。
def job_status(identity):
    if identity not in _jobs:raise ValueError("找不到安装日志；服务重启后需重新检查组件状态")
    return dict(_jobs[identity])
