# stdio MCP 的容器桥接器：在隔离容器中启动服务器，把标准输入/输出接到 MCP 协议。
# 这里的命令属于配置的 MCP 服务，不是在平台或用户宿主机任意执行 shell。

"""Trusted Docker stdio bridge. The external program receives only its own env."""
import json
import os
import signal
import sys
import threading
import time
import docker
from docker.utils.socket import frames_iter

# 从受控环境配置创建容器并转发字节流，退出时回收；协议 stdout 不应混入调试打印。
# 功能：作为 stdio MCP 的桥接进程，在受限 Docker 容器中启动外部服务并转发协议字节流。
# 输入：无参数；从 TESTPILOT_MCP_BRIDGE_CONFIG 环境变量取镜像、入口、参数、专属凭据、网络、所属任务和寿命，读取后移除该变量。
# 输出/影响：正常返回 None；创建容器、转发 stdin/stdout，退出时关闭 socket/客户端并删容器；stderr 只报告异常类型，不把外部输出抄入可信日志。
def main():
    config=json.loads(os.environ.pop("TESTPILOT_MCP_BRIDGE_CONFIG"))
    client=docker.from_env()
    container=None
    socket=None
    # 功能：响应桥接进程收到的终止信号，让外层 finally 执行容器和连接清理。
    # 输入：*_ 是 signal.signal 传入的信号编号与栈帧，函数不使用具体值。
    # 输出/影响：抛 SystemExit(0) 而非返回业务数据；不直接删容器，回收由 main 的 finally 完成。
    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    try:
        container=client.containers.create(
            config["image"],command=[config["command"],*config["args"]],entrypoint=[],
            stdin_open=True,tty=False,user="1000:1000",working_dir="/tmp",
            environment=dict(config["env"],HOME="/tmp",XDG_CACHE_HOME="/tmp/cache"),
            network=config["network"],mem_limit="512m",nano_cpus=1_000_000_000,
            pids_limit=100,cap_drop=["ALL"],security_opt=["no-new-privileges:true"],
            read_only=True,tmpfs={"/tmp":"rw,nosuid,size=128m"},init=True,
            labels={"testpilot.mcp":"true","testpilot.mcp.owner":config["owner"],"testpilot.mcp.deadline":str(time.time()+config["ttl"])},
            log_config=docker.types.LogConfig(type="none"))
        socket=container.attach_socket(params={"stdin":1,"stdout":1,"stderr":1,"stream":1})
        container.start()
        raw=getattr(socket,"_sock",socket)
        # 功能：在后台线程中将桥接进程标准输入转发给 MCP 容器，输入结束或连接报错时回收容器。
        # 输入：无参数；raw 是容器 attach socket，container 是本次创建容器，均由 main 闭包提供；从文件描述符 0 读字节。
        # 输出/影响：返回 None；持续写 socket，finally 强制删除本次容器；不解析 MCP 内容、不改平台业务记录或访问宿主项目。
        def input_stream():
            try:
                while True:
                    data=os.read(0,65536)
                    if not data:break
                    raw.sendall(data)
            except (OSError,ValueError):pass
            finally:
                try:container.remove(force=True)
                except docker.errors.DockerException:pass
        threading.Thread(target=input_stream,daemon=True).start()
        for stream,data in frames_iter(socket,tty=False):
            # 桥接进程的 stdout 只转发 MCP 协议字节，不能混入普通 print/日志；外部服务器的 stderr 也不直接复制到平台可信日志，避免泄漏其输出中的凭据。
            if stream==1:
                sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
    except SystemExit:pass
    except Exception as error:
        print("MCP container bridge failed: "+type(error).__name__,file=sys.stderr)
        raise SystemExit(1)
    finally:
        if socket:
            try:socket.close()
            except OSError:pass
        if container:
            try:container.remove(force=True)
            except docker.errors.DockerException:pass
        client.close()

if __name__=="__main__":main()
