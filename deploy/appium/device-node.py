"""Read-only Android inventory and bounded log capture; no arbitrary shell endpoint."""
import json, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

def command(args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=10)
    return p.stdout[-80000:]

class Handler(BaseHTTPRequestHandler):
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
    def log_message(self, *args): pass

ThreadingHTTPServer(("0.0.0.0", 4724), Handler).serve_forever()
