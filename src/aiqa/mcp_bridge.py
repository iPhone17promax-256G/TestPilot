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
def main():
    config=json.loads(os.environ.pop("AIQA_MCP_BRIDGE_CONFIG"))
    client=docker.from_env()
    container=None
    socket=None
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
            labels={"aiqa.mcp":"true","aiqa.mcp.owner":config["owner"],"aiqa.mcp.deadline":str(time.time()+config["ttl"])},
            log_config=docker.types.LogConfig(type="none"))
        socket=container.attach_socket(params={"stdin":1,"stdout":1,"stderr":1,"stream":1})
        container.start()
        raw=getattr(socket,"_sock",socket)
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
            # stdout is exclusively protocol. Server diagnostics stay out of trusted logs.
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
