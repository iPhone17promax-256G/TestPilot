"""Read-only Android inventory and bounded log capture; no arbitrary shell endpoint."""
import json, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# 功能：执行设备节点内部固定的 adb 或日志读取命令。
# 输入：args 是 Handler 自己构造的命令参数列表，HTTP 请求不能直接提交任意命令。
# 输出/影响：返回标准输出末尾最多 80000 字符，十秒超时抛异常；不检查退出码，本函数不提供任意 shell 拼接入口。
def command(args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=10)
    return p.stdout[-80000:]

class Handler(BaseHTTPRequestHandler):
    # 功能：处理设备节点的设备列表与日志查询。
    # 输入：self 是 BaseHTTPRequestHandler 实例，从 self.path 取 /health 或 /logs?udid= 及响应通道。
    # 输出/影响：写 HTTP JSON 或 404；执行固定 adb/tail 获取设备和最多近期日志，日志查询先核对 UDID 已登记，未提供安装/点击动作。
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            rows = []
            for line in command(["adb", "devices", "-l"]).splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    udid, state = parts[:2]
                    version = command(["adb", "-s", udid, "shell", "getprop", "ro.build.version.release"]).strip() if state == "device" else ""
                    rows.append(dict(udid=udid, state=state, androidVersion=version))
            value = dict(devices=rows, platform="android")
        elif parsed.path == "/logs":
            udid = parse_qs(parsed.query).get("udid", [""])[0]
            known = [line.split()[0] for line in command(["adb", "devices"]).splitlines()[1:] if line.strip()]
            if udid not in known:
                self.send_error(404); return
            value = dict(logcat=command(["adb", "-s", udid, "logcat", "-d", "-t", "300"]), appium=command(["tail", "-c", "40000", "/tmp/appium.log"]))
        else:
            self.send_error(404); return
        body = json.dumps(value).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    # 功能：关闭设备节点 HTTP 默认访问日志。
    # 输入：self 是请求处理器，args 是标准库传入的日志格式/值，本实现不使用。
    # 输出/影响：返回 None，函数体 pass；请求 URL 不经默认处理器日志输出，但其他组件仍可有自己的日志。
    def log_message(self, *args): pass

ThreadingHTTPServer(("0.0.0.0", 4724), Handler).serve_forever()
