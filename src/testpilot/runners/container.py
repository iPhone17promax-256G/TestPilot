# 学习导航：Test: Step7 读准备与启动容器；到容器内看 Step8～Step13；最后回来读 Test: Step14。
# 执行器学习路线：Executor: Step4 execute_run 准备输入 → Step5 launch 创建容器 → Step6 execute_run 监控与收尾。
# 接着读 local_executor.py 的 Executor: Step7，了解执行进程意外退出后怎样清理遗留容器；Step1～Step3 也在该文件。
# 首遍跟普通 API/Web 测试读，性能与 APP 分支先跳过；workspace_exec 是可选支线，不必重新学习全部测试动作。
# 可信本地执行器与不可信测试代码的边界：执行器创建容器，容器只拿到此次测试所需输入。
# Run 状态与证据在这里汇总；不要把进程退出成功直接当成测试通过。

import json, os, shutil, time, zipfile
from pathlib import Path
import docker
from ..config import settings
from ..storage import read_record, update_record
from ..testing import TERMINAL, revision, safe_path
from ..assets import version_hash

RUN_OUTPUT_LIMIT = 256 * 1024 * 1024
WORKSPACE_OUTPUT_LIMIT = 64 * 1024 * 1024
MINIMUM_DISK_FREE = 512 * 1024 * 1024
MAX_OUTPUT_FILES = 10000

# 功能：遍历可写工作区的目录条目，不跟随测试代码创建的符号链接。
# 输入：folder 是待检查的本次 Run/Task 工作目录 Path，由空间检查和收尾调用者传入。
# 输出/影响：迭代返回 (路径, 是否链接, 文件字节数)；只读文件元信息，遍历中已消失的条目跳过。
def workspace_entries(folder):
    """Walk without following links created by code in the writable mount."""
    pending=[folder]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        yield Path(entry.path), True, 0
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                        yield Path(entry.path), False, 0
                    else:
                        yield Path(entry.path), False, entry.stat(follow_symlinks=False).st_size
                except FileNotFoundError:
                    continue # 遍历工作区期间，正在运行的测试可能删除临时文件；遇到已消失的文件就跳过，不因此把整次产物大小检查判为异常。

# 运行期间检查可用磁盘、文件数和产物总量，超限停止，避免测试持续写满磁盘。
# 这是轮询检查，不是内核级文件系统硬配额，检查间隔内仍可能有短暂增长。
# 功能：检查可用磁盘、输出体积和条目数量，阻止测试继续写满磁盘。
# 输入：folder 是本次运行目录；limit 为产物字节上限，默认 256 MB，辅助工作区使用 64 MB。
# 输出/影响：返回 None 表示目前未超限；不足 512 MB、存在链接或输出超限抛错，不删除文件。
def check_storage(folder, limit=RUN_OUTPUT_LIMIT):
    if shutil.disk_usage(folder).free < MINIMUM_DISK_FREE:
        raise RuntimeError("磁盘可用空间不足 512 MB，已停止执行，请清理或备份历史产物")
    total=count=0
    for path,link,size in workspace_entries(folder):
        if link:
            raise RuntimeError("测试工作区不允许符号链接："+path.name)
        total+=size;count+=1
        if total>limit or count>MAX_OUTPUT_FILES:
            raise RuntimeError(f"工作区产物超过 {limit//1048576} MB 或 {MAX_OUTPUT_FILES} 项上限，已停止执行")

# 功能：测试停止后只删除工作区里的符号链接本身，避免读取目录外的目标内容。
# 输入：folder 为当前运行或任务工作目录，先由调用方停止测试容器。
# 输出/影响：返回删除的链接数量；修改本次目录条目，不跟随链接删除目标，不处理普通文件。
def discard_workspace_links(folder):
    # 测试容器停止后，只删除本次工作区里的符号链接条目，不跟随链接删除其目标；
    # 先做这项清理再读取日志和打包证据，避免把工作区外的文件误当作测试产物。
    removed=0
    for path,link,_ in workspace_entries(folder):
        if link:
            path.unlink(missing_ok=True);removed+=1
    return removed

# 功能：在即将输出的日志文本中隐藏当前模型密钥及已知测试密码/Token。
# 输入：text 为日志或错误字符串；秘密来源是当前 settings.model_key 和 TEST_* 凭据环境变量。
# 输出/影响：返回替换后的文本，不写文件；不扫描未知秘密或图片，不能当作完整隐私保证。
def redact(text):
    secrets=[settings.model_key]
    secrets += [v for k,v in os.environ.items() if k.startswith("TEST_") and ("PASSWORD" in k or "TOKEN" in k)]
    for secret in secrets:
        if secret:text=text.replace(secret,"[REDACTED]")
    return text

# 功能：创建数据根目录内的执行工作区，并设置容器普通用户可写的所有者。
# 输入：relative 是如 runs/RUN... 或 workspaces/TASK... 的相对目录，safe_path 限制根目录。
# 输出/影响：返回工作目录 Path；创建目录并 chown 为 1000:1000，不启动容器。
def prepare(relative):
    folder=safe_path(relative);folder.mkdir(parents=True,exist_ok=True);os.chown(folder,1000,1000)
    return folder

# Executor: Step5 [开始] launch 让 Docker Engine 创建并启动测试容器，返回供 Step6 监控的句柄。
# Docker SDK 创建独立测试容器，只把本次工作目录挂成可写。
# 设置非 root、只读根文件系统、资源上限和超时标签；不挂 Docker socket，不传模型/平台凭据。
# network=True 使用目标网络；辅助命令明确 network=False，容器里不是宿主机的完整终端。
# 辅助块 launch：relative 是数据根目录下本次运行路径，command 为命令参数列表，environment 为环境变量字典。
# 输入：network 默认True可访问目标网络；image 默认普通测试镜像；ttl为预计秒数，清理截止标签额外加30秒。
# 返回：Docker容器对象。detach=True 后进程已在容器运行，调用方拿句柄监控，不等待整个测试结束。
# 数据：宿主机 host_data/relative 挂载到 /work；限制CPU/内存/进程数、普通用户运行，不挂宿主Docker socket。
# relative 是本次运行的目录；command 是参数列表；environment 是给容器的环境变量；ttl 用于标记清理期限。
# 功能：通过 Docker Engine 创建限制权限和资源的独立测试/辅助执行容器。
# 输入：relative 为挂载工作目录；command 为命令参数数组；environment 为容器环境变量；network/image/ttl 控制网络、镜像和秒数期限。
# 输出/影响：返回已启动且分离运行的 Docker 容器对象；挂载 /work、设置资源和清理标签，不等待测试完成。
def launch(relative,command,environment,network=True,image=None,ttl=60):
    # Python 里的 docker 是另外安装的 Docker SDK，不是 Python 自带的容器功能；from_env 根据连接配置找到 Docker Engine。
    # 执行器是可信进程，有请求 Docker Engine 的权限；真正创建和启动隔离进程的是 Engine，而非把 Python 函数变成容器。
    # image 指定带执行依赖的镜像模板，例如 Python/浏览器或 JMeter；未指定用普通测试镜像，command 是容器要执行的参数数组。
    # environment 是给这次测试的环境变量，network=True 接被测网络、False 使用 none；不把模型 Key 或平台完整环境交给测试。
    # 启动准备：先检查本次工作区是否超出磁盘限制，再把执行器看到的目录换成 Docker 守护进程能挂载的宿主路径。
    # 工作目录会映射为容器里的 /work；临时配置和缓存另外放 /tmp，测试只拿到调用方传入的环境变量。
    if not settings.host_data:raise RuntimeError("TESTPILOT_HOST_DATA 未配置")
    check_storage(safe_path(relative), RUN_OUTPUT_LIMIT if relative.startswith("runs/") else WORKSPACE_OUTPUT_LIMIT)
    host=str(Path(settings.host_data)/relative)
    environment=dict(environment,HOME="/tmp",XDG_CONFIG_HOME="/tmp/config",XDG_CACHE_HOME="/tmp/cache")
    # 从这里开始真实运行：Docker 按指定镜像启动普通用户进程；只读根目录、CPU/内存/进程数上限和网络设置限制测试能做什么。
    # 调用立即返回容器句柄，Runner 继续监控；日志滚动限制体积，账号/任务目录/期限标签供异常恢复时找回并清理容器。
    # relative 是数据根目录下本次 Run/Task 的目录，挂进容器成为 /work；测试写出的证据仍保存在这个挂载目录里。
    # 容器内部不挂 Docker socket，因此用例没有执行器的 Docker 管理权限；它只拿到本次工作区和所需输入。
    # detach=True 让创建调用立即返回 Docker 容器对象，容器继续执行；它是查询状态、读日志、停止/删除容器的句柄，不是测试结果。
    # ttl 只用于算清理截止标签，标签是元信息，不会自己触发超时；运行中的停止由 Step6 监控，异常遗留由 Step7 清理。
    return docker.from_env().containers.run(image or settings.runner_image,command,detach=True,working_dir="/work",user="1000:1000",volumes={host:{"bind":"/work","mode":"rw"}},environment=environment,network=settings.target_network if network else "none",mem_limit="1g",nano_cpus=1_000_000_000,pids_limit=200,cap_drop=["ALL"],security_opt=["no-new-privileges:true"],read_only=True,tmpfs={"/tmp":"rw,nosuid,size=256m","/home/runner/.cache":"rw,nosuid,size=64m"},shm_size="256m",log_config=docker.types.LogConfig(type="json-file",config={"max-size":"10m","max-file":"2"}),labels={"testpilot.execution":"true","testpilot.executor":settings.executor_id,"testpilot.account":settings.executor_user,"testpilot.workspace":relative,"testpilot.execution.deadline":str(time.time()+ttl+30)})
    # Executor: Step5 [结束] 回到 execute_run 调用 launch 的位置，再读 Executor: Step6 的监控循环。
    # 普通 API/Web 测试中，pytest 在新容器里执行动作，Runner 在容器外拿句柄监控；性能容器运行 JMeter。

# 功能：恢复清理超期、取消或任务已结束而残留的执行容器。
# 输入：无显式参数；读取 Docker 的 testpilot.execution 标签、截止时间，以及关联 Run/Task 记录状态。
# 输出/影响：返回 None；强制删除判定应清理的容器，最后关闭 Docker Client，不删除运行证据目录。
def cleanup_execution_containers():
    """Recover auxiliary containers too, even while Agent Worker is down."""
    client=docker.from_env()
    try:
        for container in client.containers.list(filters={"label":"testpilot.execution=true"}):
            relative=container.labels.get("testpilot.workspace","")
            deadline=container.labels.get("testpilot.execution.deadline","")
            try:
                expired=bool(deadline and float(deadline)<=time.time())
                if relative.startswith("runs/"):
                    expired=expired or read_record(relative.split("/")[1],"run")["status"] in {"interrupted","cancelled"}
                elif relative.startswith("workspaces/"):
                    expired=expired or read_record(relative.split("/")[1],"task")["status"] not in {"queued","running"}
            except ValueError:
                expired=True
            if expired:
                try:container.remove(force=True)
                except docker.errors.NotFound:pass
    finally:
        client.close()

# 结合 pytest 退出码、收集结果和实际执行阶段判断：断言失败是 failed，环境/收集错误是 error。
# 零用例或全部跳过不能通过；否则一个空脚本也会被平台误当作测试成功。
# 辅助块 classify：report 是 results.json 解析成的字典，exit_code 是容器退出码，调用者是 execute_run。
# 返回：passed/failed/error/timed_out 状态字符串；不改Run，不写文件，由调用方保存判定。
# 边界：区分断言失败与准备/收集错误；无测试、缺报告、全跳过或异常退出不能判通过。
# 用真实 pytest 报告和退出码共同判断结果；进程退出并不等于实际测试通过。
# 功能：结合真实 pytest 报告和进程退出码判断测试结论，拒绝零用例或全跳过的伪通过。
# 输入：report 为 results.json 解析结果；exit_code 为容器状态中的退出码，来自 execute_run。
# 输出/影响：返回 passed/failed/error/timed_out 字符串；不更新数据库，由外层 Runner 保存结论。
# Executor 辅助跳转：Step6 读到 classify 时看这里，理解“进程退出”为什么还要核对真实报告，然后回 Step6 收尾。
def classify(report,exit_code):
    # 先确认这次执行是否有可信证据：超时单独分类；缺报告、导入/收集错误、零用例或异常退出都不能算通过。
    # 报告来自容器内 pytest 钩子写出的 results.json，这里只做判定，不采信 Agent 的口头结论。
    if report and any("from pytest-timeout" in i.get("error","") for i in report.get("items",[])):return "timed_out"
    if exit_code not in {0,1} or not report or not report.get("collected") or report.get("collectionErrors"):return "error"
    # 再区分失败发生在哪个阶段：setup/teardown 出错说明准备或清理出了问题；call 出错属于测试主体失败。
    # 只有确实执行过通过项、没有上述失败且进程正常退出才返回 passed；全部跳过没有验证实际行为。
    items=report.get("items",[])
    if any(i["outcome"]=="failed" and i["phase"]!="call" for i in items):return "error"
    if any(i["outcome"]=="failed" for i in items):return "failed"
    if not any(i["outcome"]=="passed" for i in items):return "error"
    return "passed" if exit_code==0 else "error"

# 从 Run 指定的历史版本复制输入，并核对摘要。
# 运行期间编辑同名用例只会产生新版本，不应改变这份执行输入。
# 辅助块 copy_assets：run 是冻结输入的运行字典，folder 是本次运行目录 Path，由 execute_run 传入。
# 返回：(item, rev, code)，分别为用例元信息、选定版本、源码字符串；同时把辅助文件复制到 folder。
# 数据：读 cases/用例ID/版本号/ 并核对hash，不使用用例库最新版本，也不在此直接执行源码。
# 功能：准备指定历史用例版本的输入，并核对内容摘要，保证运行时编辑不会改动本次测试。
# 输入：run 含冻结的 caseId/revision/codeHash；folder 为本次运行工作目录。
# 输出/影响：返回 (用例元信息, 版本记录, 源码字符串)；复制辅助文件到 folder，不执行源码。
def copy_assets(run,folder):
    item,rev=revision(run["caseId"],run["revision"])
    code=safe_path(rev["path"]).read_text()
    files={name:safe_path(str(Path(rev["path"]).parent/name)).read_text() for name in rev.get("files",{})}
    if version_hash(code,files,rev.get("config",{}))!=run["codeHash"]:raise RuntimeError("用例版本内容校验失败")
    for name,content in files.items():(folder/name).write_text(content)
    return item,rev,code

# 完整执行生命周期：准备目录和固定输入→启动容器→监控取消/超时→收集产物→写最终状态。
# API/Web/APP 使用 pytest；performance 使用 JMeter，最后都回到同一 Run 报告结构。
# APP 还要独占设备；Web 还会创建独立网络记录会话。
# Test: Step7 [开始] Runner 读取固定用例版本，准备目录、目标地址与 pytest/JMeter 命令。
# Executor: Step4 [开始] run_job 已选中测试 Run；这里只准备本次输入，稍后才创建测试容器。
# 职责：准备本次运行的固定输入，选择 pytest/JMeter 与可选设备，真正启动测试容器。
# 输入：run_id 是 run_job 传入的 Run ID；用它读已冻结的版本号、hash、环境地址、配置和设备快照。
# 数据来源：copy_assets 读取历史版本源码及辅助文件，并核对内容摘要，避免执行到之后编辑的新版本。
# 输出：本段得到 container 句柄、command、environment 等供 Step14 监控；execute_run 整体正常返回 None。
# 存储：准备目录为 settings.data/runs/运行ID/，写 test_case.py 或 plan.jmx、conftest.py 与辅助文件。
# 边界：BASE_URL 来自 Run 的环境快照；launch 把本次目录挂成 /work，测试从这一步才实际开始执行。
# 输入运行 ID，读已冻结的用例/环境配置，启动测试容器，监控并汇总证据。
# 概念：Runner 是 TestPilot 的测试执行实现，此处的 execute_run 负责执行生命周期；executor 是领取 Job 的进程，Runner 在它里面被调用。
# 随后 Docker SDK 用 Python 请求 Docker Engine 创建测试容器，pytest/JMeter 在容器里运行；隔离容器不是平台 API 进程，也不是新的 Agent 模型。
# settings.data 是执行进程可见的数据根目录，settings.host_data 是 Docker 守护进程可见的宿主路径；launch 把本次 runs/ID 目录挂到容器的 /work。
# 所以容器内 /work/steps.json、/work/results.json 对应容器外同一次 Run 的报告文件，容器退出后执行器仍能读取。
def execute_run(run_id):
    # run_id 来自 local_executor.run_job 的 Run 分支；执行器任务子进程在这里调用测试 Runner，不是 API 请求直接执行用例。
    # Run 已冻结 caseId/revision/环境/配置，准备到本次 runs/ID；prepare 建目录，copy_assets 取固定版本并验证摘要。
    # 准备本次执行：从 Run 取提交时冻结的用例版本和环境，已取消就停在这里；其他运行各建自己的 runs/ID 目录。
    # copy_assets 核对历史版本摘要，把配套文件复制进本次目录；之后读写的都是这次运行的输入与证据，不跟随最新编辑变化。
    run=read_record(run_id,"run")
    if run["status"] in {"cancelled","interrupted"}:return
    relative=f"runs/{run_id}";folder=prepare(relative)
    item,rev,code=copy_assets(run,folder)
    # 准备容器要用的地址和凭据：environment 是操作系统环境变量，BASE_URL 之后由 pytest fixture 提供给测试函数。
    # 只追加明确允许的测试账号/密码/Token，不把执行器的模型 Key 或完整环境交给用例；禁止生成 .pyc 以减少输出。
    # 下面这些句柄、报告和指标先留在 Runner 内存，默认结论是 error；只有后面拿到真实结果才能改为通过。
    kind=item["kind"];environment={"BASE_URL":run["environmentSnapshot"]["executionUrl"],"PYTHONDONTWRITEBYTECODE":"1"}
    environment.update({k:v for k,v in os.environ.items() if k in {"TEST_USERNAME","TEST_PASSWORD","TEST_TOKEN"}})
    device=None;container=None;metrics={};report={};logs="";resources=[];network=None
    start=time.monotonic();status="error";summary="执行器错误";exit_code=None
    try:
        image=None
        # Web 额外准备一份独立网络记录：会话归属写到业务记录，采集参数交给容器插件；实际请求文件稍后才在 network 目录产生。
        # 报告只先关联这个会话的摘要，模型需要查看正文时另调读取工具，不会自动收到整份抓包。
        if kind=="web":
            from ..network import create_session
            network=create_session(run_id,"run",relative,environment["BASE_URL"],run.get("networkCaptureLevel","api"),run.get("taskId",""))
            environment["TESTPILOT_NETWORK_CAPTURE"]=json.dumps(network)
            update_record(run_id,lambda p:p.update(network={k:v for k,v in network.items() if k!="path"}))
        if kind=="performance":
            from ..performance import materialize
            # 性能测试准备：把冻结的 JMX、当前目标地址和负载设置合成执行副本 plan.jmx；原始用例版本不会改变。
            # 下面选 JMeter 镜像并组织无界面命令，运行后分别输出 JTL 样本、日志和 HTML 报告，此时只是准备文件和参数。
            (folder/"plan.jmx").write_text(materialize(code,run["configSnapshot"],environment["BASE_URL"]))
            # 使用提交时冻结的负载参数和性能镜像，避免运行期间的编辑改变本次任务。
            # 概念：JMeter 是实际发负载的测试程序；JMX 是 XML 测试计划，辅助 CSV 提供账号等输入数据，JTL 是运行后每次请求的采样结果。
            # Python 在此把目标环境和负载配置写入执行副本，再用 JMeter -n 无界面运行；保存原始 JMX 不会立即发压测流量。
            config=run["configSnapshot"];image=settings.performance_image
            command=["-n","-t","/work/plan.jmx","-l","/work/results.jtl","-j","/work/jmeter.log","-e","-o","/work/html-report","-Jthink_ms="+str(config["think_ms"]),"-Jjmeter.save.saveservice.output_format=csv","-Jjmeter.save.saveservice.autoflush=true","-Jjmeter.save.saveservice.assertion_results_failure_message=true","-Jjmeter.save.saveservice.print_field_names=true","-Jjmeter.save.saveservice.thread_counts=true"]
        else:
            # API/Web/APP 准备：把历史源码写成 test_case.py，再生成 pytest 会自动读取的 conftest.py。
            # 这些 fixture 给测试函数提供环境地址、浏览器或 APP driver；写好文件尚未执行测试，统一在 launch 后开始。
            (folder/"test_case.py").write_text(code)
            imports="from testpilot.runners.pytest_plugin import browser_type_launch_args, base_url\n"
            if kind=="app":
                from ..mobile import acquire,capabilities
                # APP 先取得设备独占租约，避免两次测试同时操控同一部手机，再核对排队期间设备连接参数有没有改变。
                # 应用也使用冻结快照；有 APK 时重算摘要，发现内容或设备变化就拒绝旧任务，不能误测另一份安装包/另一台设备。
                # 确认后将 Appium 地址和 capabilities 交给容器 driver fixture；真正创建 Appium 会话由该 fixture 负责。
                device=acquire(run["configSnapshot"]["device_id"],run_id)
                snapshot=run["deviceSnapshot"]
                if any(device.get(k)!=snapshot.get(k) for k in ["udid","appiumUrl","nodeUrl","systemPort"]):raise ValueError("设备连接配置在提交后已改变，请重新提交测试")
                app=run["appSnapshot"]
                if app["path"]:
                    import hashlib
                    if hashlib.sha256(safe_path(app["path"]).read_bytes()).hexdigest()!=app["hash"]:raise RuntimeError("应用版本文件校验失败")
                environment.update(TESTPILOT_APPIUM_URL=device["appiumUrl"],TESTPILOT_APP_CAPABILITIES=json.dumps(capabilities(app,device,run["configSnapshot"]["reset"])))
                imports+="from testpilot.runners.app_plugin import driver\n"
            (folder/"conftest.py").write_text(imports)
            command=["python","-m","pytest","-p","testpilot.runners.pytest_plugin",rev["entry"],"-q","--tb=short","--timeout="+str(run["timeout"]),"-o","cache_dir=/tmp/pytest-cache"]
        # 正式启动：launch 把上述目录挂成 /work，pytest 或 JMeter 开始在独立容器里运行，Runner 拿到句柄继续处理。
        # 将实际镜像和引擎保存到 Run，报告可以追溯执行环境；容器里的动作流程看 Step8 起，容器外的监控看 Step14。
        # Executor: Step4 [结束] 输入文件、command 和 environment 已备好；下一步：Executor: Step5 → 本文件 launch。
        # 先读 launch 怎样申请容器，再回下面拿返回句柄；实际测试在容器内继续，执行器路线接着读 Step6。
        container=launch(relative,command,environment,image=image,ttl=run["timeout"]+15)
        update_record(run_id,lambda p:p.update(runtimeImage=container.attrs["Image"],engine="jmeter" if kind=="performance" else "appium" if kind=="app" else "pytest"))
        # Test: Step7 [结束] 下一步：Test: Step8 → src/testpilot/runners/visual_runtime.py 的 StepRuntime 开头。
        # launch 已启动隔离容器；容器里的 pytest 执行生成脚本，容器外则继续下面 Test: Step14 的监控。
        # Test: Step14 [开始] 容器外监控与收尾：读退出码/results.json，classify 判断真实测试结果。
        # Executor: Step6 [开始] launch 返回后，Runner 持续监控容器，拿真实证据判定并保存 Run。
        # 概念：容器 exit_code 只是测试程序退出码，Run.status 是 TestPilot 结合真实报告给出的运行判定；平台 HTTP 返回 200 又是另外一层结果。
        # pytest 报告缺失、零测试、全跳过、收集或准备/清理出错时不能记 passed；测试主体失败记 failed，执行环境/异常退出记 error，超过时限记 timed_out。
        # 取消/中断由用户或租约状态决定，并在迟到结果写回时保留；Agent 生成“测试通过”文字不能替换这些证据。
        # artifacts 是日志、JSON、截图等文件的索引，含名称/相对路径/大小；正文在 Run 目录，页面或工具按权限下载，数据库不保存整块图片内容。
        # 职责：在容器外监控执行，响应取消/超时，读取真实报告，清理容器，再将结果写回 Run。
        # 输入：接着使用 Step7 的 run/container/folder；pytest 分支读 results.json/steps.json，性能分支读 JTL。
        # 判定：classify(report, exit_code) 返回 passed/failed/error/timed_out；没有用例或全跳过不能通过。
        # 输出：execute_run 正常返回 None；finish(p) 修改最新 Run 字典，报告结果经 update_record 回写。
        # 存储：runs/运行ID/ 保存日志、报告、截图等产物；Run 保存状态、摘要、步骤/整体结果和 artifacts 文件索引。
        # 存储：执行器先改本 Job SQLite，再经 replica 回传平台；产物文件另行上传，不是数据库里的大块图片。
        # 边界：finally 清理容器/设备；写回尊重用户取消。性能资源统计指施压容器，不是被测服务的 CPU/内存。
        # 两个计时点分别控制性能指标刷新和输出大小检查，避免每次轮询都做重工作。
        # 持续监控：可信 Runner 在容器外轮询，因此用例卡住也仍可停止；每次检查最新 Run 的取消状态和整次运行时限。
        # 输出目录约每秒检查大小，APP 同时续设备租约并登记原生会话；两项计时不影响容器内正在执行的动作。
        # 这里不是逐个调用测试步骤：整个测试已在容器里运行，reload 只刷新句柄中的状态；停止请求和超时由容器外处理。
        refresh_at=0;storage_at=0
        while True:
            current=read_record(run_id)
            if current["status"] in {"cancelled","interrupted"}:
                status,summary=current["status"],current["summary"];container.stop(timeout=2);break
            if time.monotonic()-start>run["timeout"]+15:
                status,summary="timed_out","超过整次运行时间限制";container.stop(timeout=2);break
            if time.monotonic()>=storage_at:
                check_storage(folder)
                storage_at=time.monotonic()+1
            if device:
                from ..mobile import renew
                session_file=folder/"appium-sessions.json";sessions=json.loads(session_file.read_text()) if session_file.exists() else []
                renew(device["id"],run_id,sessions[-1] if sessions else "")
            container.reload()
            # 压测进行中约每 3 秒读 JTL 样本并刷新 Run，页面因此能看到请求数、吞吐、失败率和 P95。
            # P95 是约 95% 样本不超过的耗时，是否达标还需配置阈值；这里取的 Docker CPU/内存属于施压容器，不是被测服务。
            # CPU 比例按前后累计时间差计算，资源曲线暂存在内存，和请求样本指标分开保存。
            if kind=="performance" and time.monotonic()>refresh_at:
                from ..performance import read_metrics
                metrics=read_metrics(folder/"results.jtl",run["configSnapshot"])
                try:
                    stats=container.stats(stream=False)
                    cpu=stats["cpu_stats"];previous=stats["precpu_stats"]
                    delta=cpu["cpu_usage"]["total_usage"]-previous["cpu_usage"]["total_usage"]
                    system=cpu.get("system_cpu_usage",0)-previous.get("system_cpu_usage",0)
                    resources.append({"second":round(time.monotonic()-start,1),"cpuPercent":round(delta/system*cpu.get("online_cpus",1)*100,2) if system>0 else 0,"memoryMB":round(stats["memory_stats"].get("usage",0)/1048576,1)})
                except docker.errors.DockerException:pass
                update_record(run_id,lambda p:p.update(metrics=metrics,resources=resources[-900:],summary=f"正在压测：{metrics['samples']} 个样本"))
                refresh_at=time.monotonic()+3
            # 容器结束后才判最终结果：性能分支检查退出码、有效样本、统计是否完整和阈值；没设阈值只能说完成。
            # pytest 分支限大小读取 results.json 交给 classify，并补充导入失败、零用例或全跳过等原因；退出码 0 本身不是通过证据。
            # 尚未结束就稍后再查，退出监控后再取末尾日志，避免无限日志撑大报告。
            if container.status in {"exited","dead"}:
                check_storage(folder)
                exit_code=container.attrs["State"]["ExitCode"]
                if kind=="performance":
                    from ..performance import read_metrics
                    metrics=read_metrics(folder/"results.jtl",run["configSnapshot"],final=True)
                    if metrics.get("truncated"):
                        status,summary="error","结果超过一百万条处理上限，统计不完整；请缩短时长或减少负载"
                    elif exit_code!=0 or not metrics["samples"]:
                        status,summary="error","JMeter 执行失败或没有有效样本"
                    elif metrics["verdict"]=="not_evaluated":status,summary="completed","压测已完成，未配置响应时间或失败率达标条件"
                    else:
                        status=metrics["verdict"];summary="性能达标条件全部满足" if status=="passed" else "性能指标未达标"
                else:
                    # 普通 API/Web 测试重点看这条分支：容器内插件写 results.json，Runner 读它再结合退出码调用 classify。
                    # classify 的小函数在本文件上方；只靠 HTTP 200、进程退出 0 或模型说“通过”都不能代替真实测试证据。
                    report_path=folder/"results.json"
                    if report_path.exists() and report_path.stat().st_size<2_000_000:report=json.loads(report_path.read_text())
                    status=classify(report,exit_code)
                    summary={"passed":"实际执行的测试已通过","failed":"测试断言失败","error":"执行器或测试准备发生错误","timed_out":"测试超过配置的时间限制"}[status]
                    if status=="error":
                        if report.get("collectionErrors"):summary="测试代码导入或收集失败"
                        elif not report.get("collected"):summary="未收集到可执行的测试"
                        elif report.get("items") and all(i["outcome"]=="skipped" for i in report["items"]):summary="全部测试被跳过，没有验证实际行为"
                break
            time.sleep(.7)
        logs=redact(container.logs().decode(errors="replace")[-120000:])
    # 准备或监控异常：保存脱敏且限长的原因及已有容器日志，归为执行 error，让用户知道测试没有可靠完成。
    # 成功、失败或异常随后都进入 finally；清理不会依赖用例自己正常退出。
    except Exception as error:
        status,summary="error","执行准备或运行失败："+redact(str(error))[:500]
        logs=redact(f"{type(error).__name__}: {error}")[:12000]
        if container:
            try:logs+="\n"+redact(container.logs().decode(errors="replace")[-120000:])
            except docker.errors.DockerException:pass
    # 统一清理：回收测试容器，只删本次目录里的符号链接本身，防止把目录外文件当成证据读取。
    # APP 还关闭已登记的原生会话、释放设备租约，并尽力取得节点日志；清理完其他任务才能安全使用设备。
    finally:
        # remove(force=True) 回收本次容器，不删挂载在宿主机的 runs/ID；测试进程结束后，报告和截图仍可读取。
        if container:
            try:container.remove(force=True)
            except docker.errors.DockerException:pass
        if discard_workspace_links(folder):
            status,summary="error","工作区包含符号链接，已移除链接并停止处理；没有读取链接目标"
            report={};logs+="\n"+summary
        if device:
            from ..mobile import release,stop_session
            session_file=folder/"appium-sessions.json"
            if session_file.exists():
                for native in json.loads(session_file.read_text()):stop_session(device["appiumUrl"],native)
            release(device["id"],run_id)
            if device.get("nodeUrl"):
                try:
                    import httpx
                    response=httpx.get(device["nodeUrl"]+"/logs",params={"udid":device["udid"]},timeout=12);response.raise_for_status()
                    for key,text in response.json().items():(folder/(key+".log")).write_text(redact(text))
                except Exception:pass
    if kind=="performance":
        from ..performance import read_metrics
        metrics=read_metrics(folder/"results.jtl",run["configSnapshot"],final=True)
        # 整理压测产物：将最终统计写入 metrics.json，有 HTML 报告时连资源一起压成 ZIP，方便下载后完整查看。
        # 这些文件都留在当前 Run 目录；接下来普通测试和性能测试共用日志、产物索引及结果写回过程。
        (folder/"metrics.json").write_text(json.dumps(metrics,ensure_ascii=False))
        if (folder/"html-report").is_dir():
            with zipfile.ZipFile(folder/"jmeter-report.zip","w",zipfile.ZIP_DEFLATED) as archive:
                for path in (folder/"html-report").rglob("*"):
                    if path.is_file():archive.write(path,str(path.relative_to(folder/"html-report")))
    # 整理共用证据：日志写 execution.log，结构化报告应用已有敏感值规则；这不保证截图等所有产物都已脱敏。
    # 只给合规大小的顶层产物建立名称/路径/大小索引，排除输入源码和会话文件；文件正文留在目录，不塞进数据库图片字段。
    (folder/"execution.log").write_text(logs)
    report=json.loads(redact(json.dumps(report,ensure_ascii=False)))
    artifacts=[]
    excluded={"test_case.py","plan.jmx","conftest.py","appium-sessions.json",*rev.get("files",{}).keys()}
    for path in folder.iterdir():
        if path.is_file() and path.name not in excluded and path.stat().st_size<=100_000_000:
            artifacts.append({"name":path.name,"path":f"{relative}/{path.name}","url":f"/api/artifacts/{relative}/{path.name}","size":path.stat().st_size})
    # 收尾回写时尊重已经记录的取消状态，不能因为迟到的结果把“已取消”覆盖成“通过”。
    # 子块 finish：p 是 update_record 传入的最新 Run 字典，不是函数开头的旧快照。
    # 输出：无返回值；原地补充状态、耗时、整体/步骤证据、日志和产物索引，外层 update_record 持久化。
    # 边界：若p已取消/中断，保留该状态；不会因稍后采集到“passed”就覆盖用户的停止操作。
    # p 是写入时最新的运行记录；与函数开头的快照不同，可以看到期间发生的取消。
    # 功能：作为 update_record 回调汇总本次 Run 结果，同时尊重更新时最新的取消状态。
    # 输入：p 为 storage 传来的最新 Run 字典；状态、报告、日志、目录和指标来自外层 execute_run 闭包。
    # 输出/影响：返回 None；原地更新 p 的结果与产物索引，由外层 update_record 持久化，已取消/中断不被迟到结果覆盖。
    def finish(p):
        # 写回使用最新 Run：保留期间发生的取消/中断，再一起补全实际报告、动作结果、耗时、日志及产物索引。
        # p 只是回调拿到的业务字典，update_record 随后负责保存；性能统计另外补进同一条 Run，不用 Agent 文字替代证据。
        final_status,final_summary=(p["status"],p["summary"]) if p["status"] in {"cancelled","interrupted"} else (status,summary)
        p.update(status=final_status,summary=final_summary,duration=f"{time.monotonic()-start:.2f}s",items=report.get("items",[]),collectionErrors=report.get("collectionErrors",[]),collected=report.get("collected",0),artifacts=artifacts,logs=logs,exitCode=exit_code,steps=json.loads((folder/"steps.json").read_text()) if (folder/"steps.json").exists() else [])
        if kind=="performance":p.update(metrics=metrics,resources=resources)
    # 网络证据单独收尾并关联 Run，读取失败只注明采集错误，不伪造抓包，也不覆盖真实测试结论。
    # 随后 finish 写入整体结果；执行器先保存到本 Job 的 SQLite，再由 replica 回传平台，产物文件另行同步。
    if network:
        from ..network import session_summary
        try:
            summary_capture=session_summary(network["id"],finish=True)
        except (ValueError,OSError,json.JSONDecodeError) as error:
            summary_capture=dict(id=network["id"],level=network["level"],status="error",count=0,error="网络记录不可用："+str(error)[:200])
        update_record(run_id,lambda p:p.update(network=summary_capture))
    update_record(run_id,finish)
    # Executor: Step6 [结束] 监控、正常清理和结果保存到此；update_record 已经走副本/HTTP 同步，随后才返回 run_job。
    # 下一步：Executor: Step7 → src/testpilot/local_executor.py 的 consumer，找 if time.monotonic()>cleanup_at，了解过期容器清理。
    # Test: Step14 [结束] 下一步：Test: Step15 → src/testpilot/replica.py 的 transaction。
    # update_record 走 storage.session；执行器角色用 HTTP 同步结果，不能把本地 SQLite 当平台 MySQL。

# Agent 辅助脚本入口：argv 是参数数组，直接作为容器命令，不拼接宿主机 shell 命令。
# 无网络、无模型 Key、限定当前任务工作区；返回输出与退出码，帮助 Agent 继续判断。
# 辅助命令不是正式 Run，不会自动成为用例或测试通过证据；正式测试仍须保存版本并 runs_start。
# 功能：让 Agent 在自己任务工作区的隔离容器中执行辅助命令，区别于正式测试 Run。
# 输入：task_id 为当前任务 ID；argv 是命令及参数数组；timeout 为秒数并限定在 1 到 60 秒。
# 输出/影响：返回 exitCode/output 字典；无网络容器可修改本任务工作区，输出限长和脱敏，finally 清理容器及链接。
# Executor 可选支线 [开始] Agent 要执行辅助命令时，也使用 launch 建隔离容器；无需把它算成新的主路线 Step。
def workspace_exec(task_id,argv,timeout=30):
    # task_id 指定 Agent 自己的 workspaces/ID，argv 如 ["python", "analyze.py"] 指定工作区程序；不操作宿主机终端。
    # 和正式 Run 共用容器隔离，但 network=False，不注入模型 Key，最多运行 60 秒；可写区域是本任务挂载目录和容器临时目录。
    # 返回退出码与有限输出供 Agent 决定下一步，不产生正式用例版本/Run，也不能据此宣布回归测试已通过。
    if not argv or len(argv)>40 or any(not isinstance(a,str) for a in argv):raise ValueError("命令需要由参数列表组成")
    relative=f"workspaces/{task_id}";prepare(relative)
    container=launch(relative,argv,{"PYTHONDONTWRITEBYTECODE":"1"},network=False,ttl=min(max(timeout,1),60))
    start=time.monotonic()
    try:
        while time.monotonic()-start<min(max(timeout,1),60):
            check_storage(safe_path(relative),WORKSPACE_OUTPUT_LIMIT)
            container.reload()
            if container.status=="exited":return {"exitCode":container.attrs["State"]["ExitCode"],"output":redact(container.logs().decode(errors="replace")[-16000:])}
            if read_record(task_id)["status"] in {"cancelled","interrupted"}:raise RuntimeError("任务已取消")
            time.sleep(.3)
        return {"exitCode":124,"output":"命令超过时间限制"}
    finally:
        try:container.remove(force=True)
        except docker.errors.NotFound:pass
        discard_workspace_links(safe_path(relative))
    # Executor 可选支线 [结束] 输出交回 Agent，finally 已回收容器；正式测试仍要保存用例并提交 Run。
