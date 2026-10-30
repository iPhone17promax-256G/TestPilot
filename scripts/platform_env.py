"""开发环境的双模式配置生成器，不输出凭据；发布包使用独立安装配置。"""
import os
import secrets
from pathlib import Path
root=Path(__file__).resolve().parent.parent
prior=root/"deploy/platform.env"
existing=dict(line.split("=",1) for line in prior.read_text().splitlines() if "=" in line) if prior.exists() else {}
mode=os.environ.get("TESTPILOT_DEPLOYMENT_MODE",existing.get("TESTPILOT_DEPLOYMENT_MODE","local"))
if mode not in {"local","server"}:raise SystemExit("部署模式只能是 local/server")
allowed={"TESTPILOT_DATABASE_URL","TESTPILOT_ACCESS_TOKEN","TESTPILOT_DB_PASSWORD","TESTPILOT_DB_ROOT_PASSWORD","TESTPILOT_REDIS_PASSWORD","TESTPILOT_REDIS_URL","TESTPILOT_QDRANT_URL","TESTPILOT_QDRANT_API_KEY"}
lines=[line for line in (root/".env").read_text().splitlines() if line.split("=",1)[0] in allowed]
token=existing.get("TESTPILOT_MANAGED_TOKEN") or secrets.token_urlsafe(48)
common=["TESTPILOT_DEPLOYMENT_MODE="+mode,"TESTPILOT_MANAGED_EXECUTOR=true","TESTPILOT_MANAGED_TOKEN="+token]
tls=Path(os.environ.get("TESTPILOT_TLS_DIR",existing.get("TESTPILOT_TLS_DIR",str(root/".tls")))).expanduser().resolve()
if any(char in str(tls) for char in "\r\n$"):raise SystemExit("证书目录不能包含换行或 $")
# 证书目录独立于 .runtime/.client，避免整体业务目录挂载时把 CA 签名私钥带进容器。
for parent in [root/".runtime",root/".client"]:
 if tls.is_relative_to(parent.resolve()):raise SystemExit("证书目录不能放在整体挂载的 .runtime 或 .client 下")
tls.mkdir(parents=True,exist_ok=True,mode=0o700)
os.chmod(tls,0o700)
for path,content in [(prior,lines+common+["TESTPILOT_ROLE=platform","TESTPILOT_TLS_DIR="+str(tls),"TESTPILOT_CLIENT_HOST_ROOT="+str(root/".client")]),(root/"deploy/runtime.env",common)]:
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
 with os.fdopen(fd,"w") as file:file.write("\n".join(content)+"\n")
 os.chmod(path,0o600)
(root/".client/apks").mkdir(parents=True,exist_ok=True)
os.chmod(root/".client",0o700)
print("部署模式："+mode+"；自动执行器已配置。")
