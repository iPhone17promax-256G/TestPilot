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
                    continue # The running test may remove an intermediate file.

# 运行期间检查可用磁盘、文件数和产物总量，超限停止，避免测试持续写满磁盘。
# 这是轮询检查，不是内核级文件系统硬配额，检查间隔内仍可能有短暂增长。
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

def discard_workspace_links(folder):
    # Called after the container stops. Remove only link entries in its own
    # mount, never their targets, before reading logs or packaging evidence.
    removed=0
    for path,link,_ in workspace_entries(folder):
        if link:
            path.unlink(missing_ok=True);removed+=1
    return removed

def redact(text):
    secrets=[settings.model_key]
    secrets += [v for k,v in os.environ.items() if k.startswith("TEST_") and ("PASSWORD" in k or "TOKEN" in k)]
    for secret in secrets:
        if secret:text=text.replace(secret,"[REDACTED]")
    return text

def prepare(relative):
    folder=safe_path(relative);folder.mkdir(parents=True,exist_ok=True);os.chown(folder,1000,1000)
    return folder

# Docker SDK 创建独立测试容器，只把本次工作目录挂成可写。
# 设置非 root、只读根文件系统、资源上限和超时标签；不挂 Docker socket，不传模型/平台凭据。
# network=True 使用目标网络；辅助命令明确 network=False，容器里不是宿主机的完整终端。
def launch(relative,command,environment,network=True,image=None,ttl=60):
    if not settings.host_data:raise RuntimeError("AIQA_HOST_DATA 未配置")
    check_storage(safe_path(relative), RUN_OUTPUT_LIMIT if relative.startswith("runs/") else WORKSPACE_OUTPUT_LIMIT)
    host=str(Path(settings.host_data)/relative)
    environment=dict(environment,HOME="/tmp",XDG_CONFIG_HOME="/tmp/config",XDG_CACHE_HOME="/tmp/cache")
    return docker.from_env().containers.run(image or settings.runner_image,command,detach=True,working_dir="/work",user="1000:1000",volumes={host:{"bind":"/work","mode":"rw"}},environment=environment,network=settings.target_network if network else "none",mem_limit="1g",nano_cpus=1_000_000_000,pids_limit=200,cap_drop=["ALL"],security_opt=["no-new-privileges:true"],read_only=True,tmpfs={"/tmp":"rw,nosuid,size=256m","/home/runner/.cache":"rw,nosuid,size=64m"},shm_size="256m",log_config=docker.types.LogConfig(type="json-file",config={"max-size":"10m","max-file":"2"}),labels={"aiqa.execution":"true","aiqa.executor":settings.executor_id,"aiqa.account":settings.executor_user,"aiqa.workspace":relative,"aiqa.execution.deadline":str(time.time()+ttl+30)})

def cleanup_execution_containers():
    """Recover auxiliary containers too, even while Agent Worker is down."""
    client=docker.from_env()
    try:
        for container in client.containers.list(filters={"label":"aiqa.execution=true"}):
            relative=container.labels.get("aiqa.workspace","")
            deadline=container.labels.get("aiqa.execution.deadline","")
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
def classify(report,exit_code):
    if report and any("from pytest-timeout" in i.get("error","") for i in report.get("items",[])):return "timed_out"
    if exit_code not in {0,1} or not report or not report.get("collected") or report.get("collectionErrors"):return "error"
    items=report.get("items",[])
    if any(i["outcome"]=="failed" and i["phase"]!="call" for i in items):return "error"
    if any(i["outcome"]=="failed" for i in items):return "failed"
    if not any(i["outcome"]=="passed" for i in items):return "error"
    return "passed" if exit_code==0 else "error"

# 从 Run 指定的历史版本复制输入，并核对摘要。
# 运行期间编辑同名用例只会产生新版本，不应改变这份执行输入。
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
def execute_run(run_id):
    run=read_record(run_id,"run")
    if run["status"] in {"cancelled","interrupted"}:return
    relative=f"runs/{run_id}";folder=prepare(relative)
    item,rev,code=copy_assets(run,folder)
    kind=item["kind"];environment={"BASE_URL":run["environmentSnapshot"]["executionUrl"],"PYTHONDONTWRITEBYTECODE":"1"}
    # 凭据采用白名单：只传被测系统测试账号相关变量，不把本地执行器完整环境传入容器。
    environment.update({k:v for k,v in os.environ.items() if k in {"TEST_USERNAME","TEST_PASSWORD","TEST_TOKEN"}})
    device=None;container=None;metrics={};report={};logs="";resources=[];network=None
    start=time.monotonic();status="error";summary="执行器错误";exit_code=None
    try:
        image=None
        if kind=="web":
            from ..network import create_session
            network=create_session(run_id,"run",relative,environment["BASE_URL"],run.get("networkCaptureLevel","api"),run.get("taskId",""))
            environment["AIQA_NETWORK_CAPTURE"]=json.dumps(network)
            update_record(run_id,lambda p:p.update(network={k:v for k,v in network.items() if k!="path"}))
        if kind=="performance":
            from ..performance import materialize
            (folder/"plan.jmx").write_text(materialize(code,run["configSnapshot"],environment["BASE_URL"]))
            config=run["configSnapshot"];image=settings.performance_image
            command=["-n","-t","/work/plan.jmx","-l","/work/results.jtl","-j","/work/jmeter.log","-e","-o","/work/html-report","-Jthink_ms="+str(config["think_ms"]),"-Jjmeter.save.saveservice.output_format=csv","-Jjmeter.save.saveservice.autoflush=true","-Jjmeter.save.saveservice.assertion_results_failure_message=true","-Jjmeter.save.saveservice.print_field_names=true","-Jjmeter.save.saveservice.thread_counts=true"]
        else:
            (folder/"test_case.py").write_text(code)
            imports="from aiqa.runners.pytest_plugin import browser_type_launch_args, base_url\n"
            if kind=="app":
                from ..mobile import acquire,capabilities
                device=acquire(run["configSnapshot"]["device_id"],run_id)
                snapshot=run["deviceSnapshot"]
                if any(device.get(k)!=snapshot.get(k) for k in ["udid","appiumUrl","nodeUrl","systemPort"]):raise ValueError("设备连接配置在提交后已改变，请重新提交测试")
                app=run["appSnapshot"]
                if app["path"]:
                    import hashlib
                    if hashlib.sha256(safe_path(app["path"]).read_bytes()).hexdigest()!=app["hash"]:raise RuntimeError("应用版本文件校验失败")
                environment.update(AIQA_APPIUM_URL=device["appiumUrl"],AIQA_APP_CAPABILITIES=json.dumps(capabilities(app,device,run["configSnapshot"]["reset"])))
                imports+="from aiqa.runners.app_plugin import driver\n"
            (folder/"conftest.py").write_text(imports)
            command=["python","-m","pytest","-p","aiqa.runners.pytest_plugin",rev["entry"],"-q","--tb=short","--timeout="+str(run["timeout"]),"-o","cache_dir=/tmp/pytest-cache"]
        container=launch(relative,command,environment,image=image,ttl=run["timeout"]+15)
        update_record(run_id,lambda p:p.update(runtimeImage=container.attrs["Image"],engine="jmeter" if kind=="performance" else "appium" if kind=="app" else "pytest"))
        refresh_at=0;storage_at=0
        # 监控由容器外可信进程完成：用例自身卡住或不主动退出，也能被取消、超时或资源检查终止。
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
            if kind=="performance" and time.monotonic()>refresh_at:
                from ..performance import read_metrics
                metrics=read_metrics(folder/"results.jtl",run["configSnapshot"])
                try:
                    # 这些 CPU/内存来自施压容器，不是被测 MM 服务，报告解释时要区分观察对象。
                    stats=container.stats(stream=False)
                    cpu=stats["cpu_stats"];previous=stats["precpu_stats"]
                    delta=cpu["cpu_usage"]["total_usage"]-previous["cpu_usage"]["total_usage"]
                    system=cpu.get("system_cpu_usage",0)-previous.get("system_cpu_usage",0)
                    resources.append({"second":round(time.monotonic()-start,1),"cpuPercent":round(delta/system*cpu.get("online_cpus",1)*100,2) if system>0 else 0,"memoryMB":round(stats["memory_stats"].get("usage",0)/1048576,1)})
                except docker.errors.DockerException:pass
                update_record(run_id,lambda p:p.update(metrics=metrics,resources=resources[-900:],summary=f"正在压测：{metrics['samples']} 个样本"))
                refresh_at=time.monotonic()+3
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
    except Exception as error:
        status,summary="error","执行准备或运行失败："+redact(str(error))[:500]
        logs=redact(f"{type(error).__name__}: {error}")[:12000]
        if container:
            try:logs+="\n"+redact(container.logs().decode(errors="replace")[-120000:])
            except docker.errors.DockerException:pass
    # 即使断言/准备/采集失败也执行清理：回收容器和设备租约，避免下一次运行受到残留影响。
    finally:
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
        (folder/"metrics.json").write_text(json.dumps(metrics,ensure_ascii=False))
        if (folder/"html-report").is_dir():
            with zipfile.ZipFile(folder/"jmeter-report.zip","w",zipfile.ZIP_DEFLATED) as archive:
                for path in (folder/"html-report").rglob("*"):
                    if path.is_file():archive.write(path,str(path.relative_to(folder/"html-report")))
    (folder/"execution.log").write_text(logs)
    report=json.loads(redact(json.dumps(report,ensure_ascii=False)))
    artifacts=[]
    excluded={"test_case.py","plan.jmx","conftest.py","appium-sessions.json",*rev.get("files",{}).keys()}
    for path in folder.iterdir():
        if path.is_file() and path.name not in excluded and path.stat().st_size<=100_000_000:
            artifacts.append({"name":path.name,"path":f"{relative}/{path.name}","url":f"/api/artifacts/{relative}/{path.name}","size":path.stat().st_size})
    # 收尾回写时尊重已经记录的取消状态，不能因为迟到的结果把“已取消”覆盖成“通过”。
    def finish(p):
        final_status,final_summary=(p["status"],p["summary"]) if p["status"] in {"cancelled","interrupted"} else (status,summary)
        p.update(status=final_status,summary=final_summary,duration=f"{time.monotonic()-start:.2f}s",items=report.get("items",[]),collectionErrors=report.get("collectionErrors",[]),collected=report.get("collected",0),artifacts=artifacts,logs=logs,exitCode=exit_code,steps=json.loads((folder/"steps.json").read_text()) if (folder/"steps.json").exists() else [])
        if kind=="performance":p.update(metrics=metrics,resources=resources)
    if network:
        from ..network import session_summary
        try:
            summary_capture=session_summary(network["id"],finish=True)
        except (ValueError,OSError,json.JSONDecodeError) as error:
            summary_capture=dict(id=network["id"],level=network["level"],status="error",count=0,error="网络记录不可用："+str(error)[:200])
        update_record(run_id,lambda p:p.update(network=summary_capture))
    update_record(run_id,finish)

# Agent 辅助脚本入口：argv 是参数数组，直接作为容器命令，不拼接宿主机 shell 命令。
# 无网络、无模型 Key、限定当前任务工作区；返回输出与退出码，帮助 Agent 继续判断。
# 辅助命令不是正式 Run，不会自动成为用例或测试通过证据；正式测试仍须保存版本并 runs_start。
def workspace_exec(task_id,argv,timeout=30):
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
