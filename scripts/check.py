#!/usr/bin/env python3
"""Check a release against a disposable MySQL database, never the demo data."""
import secrets
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(arguments, **kwargs):
    return subprocess.run(arguments, cwd=ROOT, check=True, **kwargs)


def database(sql):
    # The MySQL container supplies its root password internally; it is never
    # interpolated into host commands or printed in output.
    run(["docker", "exec", "aiqa-mysql-1", "sh", "-c",
         'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -e "$1"', "aiqa-check", sql],
        stdout=subprocess.DEVNULL)


def backend():
    database_name = "aiqa_checks_" + secrets.token_hex(8)
    container_name = database_name.replace("_", "-")
    created = False
    granted = False
    try:
        database(f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4;")
        created = True
        database(f"GRANT ALL PRIVILEGES ON `{database_name}`.* TO 'aiqa'@'%';")
        granted = True
        program = f"""import os
from sqlalchemy.engine import make_url
os.environ['AIQA_DATABASE_URL']=make_url(os.environ['AIQA_DATABASE_URL']).set(database={database_name!r}).render_as_string(hide_password=False)
from aiqa.storage import initialize
initialize()
from aiqa.testing import seed
seed()
import pytest
raise SystemExit(pytest.main(['-q','/audit/tests','-o','cache_dir=/tmp/aiqa-pytest-cache']))
"""
        arguments = ["docker", "create", "--name", container_name, "--network", "aiqa_management",
                     "--env-file", str(ROOT / ".env"), "-e", "AIQA_DATA=/audit-data", "-e", "AIQA_ROLE=legacy",
                     "-e", "PYTHONPATH=/app/src", "-e", "PYTHONDONTWRITEBYTECODE=1",
                     "-e", "AIQA_ACCESS_TOKEN=aiqa-check-placeholder",
                     "-e", "AIQA_MODEL_KEY=aiqa-check-placeholder",
                     "-e", "AIQA_MODEL_URL=http://model.invalid/v1",
                     "-e", "TEST_USERNAME=", "-e", "TEST_PASSWORD=", "-e", "TEST_TOKEN=",
                     "--tmpfs", "/audit-data:rw,size=256m",
                     "-v", str(ROOT / "src") + ":/app/src:ro",
                     "-v", str(ROOT / "tests") + ":/audit/tests:ro",
                     "-v", str(ROOT / "deploy/package") + ":/audit/deploy/package:ro",
                     "-v", "/var/run/docker.sock:/var/run/docker.sock",
                     "aiqa-runtime:0.1", "python", "-c", program]
        run(arguments, stdout=subprocess.DEVNULL)
        run(["docker", "network", "connect", "aiqa-targets", container_name])
        print("检查后端：独立数据库、临时文件目录，模型使用占位配置。", flush=True)
        run(["docker", "start", "-a", container_name])
        code = int(subprocess.check_output(["docker", "inspect", "-f", "{{.State.ExitCode}}", container_name], text=True).strip())
        if code:
            raise subprocess.CalledProcessError(code, "backend checks")
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if created:
            try:
                if granted:
                    database(f"REVOKE ALL PRIVILEGES ON `{database_name}`.* FROM 'aiqa'@'%';")
            finally:
                database(f"DROP DATABASE `{database_name}`;")


def main():
    if not shutil.which("docker") or not shutil.which("npm") or not (ROOT / ".env").is_file():
        raise SystemExit("请先在 WSL 安装 Docker、Node.js 并运行 bash scripts/start.sh，再执行 python3 scripts/check.py。")
    backend()
    for arguments in [["npm", "test"], ["npm", "run", "build"]]:
        subprocess.run(arguments, cwd=ROOT / "web", check=True)
    print("全部检查通过；临时数据库、权限和测试容器已清理。")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode)
