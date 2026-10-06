"""开发环境的双模式配置生成器，不输出凭据；发布包使用独立安装配置。"""
import os
import secrets
from pathlib import Path
root=Path(__file__).resolve().parent.parent
prior=root/"deploy/platform.env"
existing=dict(line.split("=",1) for line in prior.read_text().splitlines() if "=" in line) if prior.exists() else {}
mode=os.environ.get("AIQA_DEPLOYMENT_MODE",existing.get("AIQA_DEPLOYMENT_MODE","local"))
if mode not in {"local","server"}:raise SystemExit("部署模式只能是 local/server")
allowed={"AIQA_DATABASE_URL","AIQA_ACCESS_TOKEN","AIQA_DB_PASSWORD","AIQA_DB_ROOT_PASSWORD"}
lines=[line for line in (root/".env").read_text().splitlines() if line.split("=",1)[0] in allowed]
token=existing.get("AIQA_MANAGED_TOKEN") or secrets.token_urlsafe(48)
common=["AIQA_DEPLOYMENT_MODE="+mode,"AIQA_MANAGED_EXECUTOR=true","AIQA_MANAGED_TOKEN="+token]
for path,content in [(prior,lines+common+["AIQA_ROLE=platform"]),(root/"deploy/runtime.env",common)]:
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
 with os.fdopen(fd,"w") as file:file.write("\n".join(content)+"\n")
 os.chmod(path,0o600)
(root/".client/apks").mkdir(parents=True,exist_ok=True)
os.chmod(root/".client",0o700)
print("部署模式："+mode+"；自动执行器已配置。")
