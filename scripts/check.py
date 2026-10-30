#!/usr/bin/env python3
"""Check a release against a disposable MySQL database, never the demo data."""
import secrets
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# 功能：在项目根目录执行检查所需子命令。
# 输入：arguments 是命令参数列表，kwargs 传给 subprocess.run，例如 stdout。
# 输出/影响：返回 CompletedProcess；check=True 使失败抛异常，命令的实际副作用由 arguments 决定。
def run(arguments, **kwargs):
    return subprocess.run(arguments, cwd=ROOT, check=True, **kwargs)


# 功能：通过已有 MySQL 容器执行验收 SQL。
# 输入：sql 是本脚本生成的建库/授权/清理语句，固定容器中的环境变量提供 root 密码。
# 输出/影响：返回 None；可创建或删除临时验收库、增删临时授权，不输出密码。
def database(sql):
    # MySQL 检查命令在数据库容器内读取自己的 root 密码，不把密码值拼入宿主机命令，
    # 也不把该密码打印到检查输出；这里只输出检查结果，不能据此推断所有测试日志都自动脱敏。
    run(["docker", "exec", "testpilot-mysql-1", "sh", "-c",
         'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -e "$1"', "testpilot-check", sql],
        stdout=subprocess.DEVNULL)


# 功能：用独立数据库和临时目录运行后端测试。
# 输入：无参数；读取安装 .env 和运行镜像/源码/测试目录。
# 输出/影响：创建随机验收库与测试容器，运行 pytest 并核对退出码；finally 撤销临时授权、删库和删容器，不使用日常业务数据库。
def backend():
    database_name = "testpilot_checks_" + secrets.token_hex(8)
    container_name = database_name.replace("_", "-")
    created = False
    granted = False
    try:
        database(f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4;")
        created = True
        database(f"GRANT ALL PRIVILEGES ON `{database_name}`.* TO 'testpilot'@'%';")
        granted = True
        program = f"""import os
from sqlalchemy.engine import make_url
os.environ['TESTPILOT_DATABASE_URL']=make_url(os.environ['TESTPILOT_DATABASE_URL']).set(database={database_name!r}).render_as_string(hide_password=False)
from testpilot.storage import initialize
initialize()
from testpilot.testing import seed
seed()
import pytest
raise SystemExit(pytest.main(['-q','/audit/tests','-o','cache_dir=/tmp/testpilot-pytest-cache']))
"""
        arguments = ["docker", "create", "--name", container_name, "--network", "testpilot_management",
                     "--env-file", str(ROOT / ".env"), "-e", "TESTPILOT_DATA=/audit-data", "-e", "TESTPILOT_ROLE=legacy",
                     "-e", "PYTHONPATH=/app/src", "-e", "PYTHONDONTWRITEBYTECODE=1",
                     "-e", "TESTPILOT_ACCESS_TOKEN=testpilot-check-placeholder",
                     "-e", "TESTPILOT_MODEL_KEY=testpilot-check-placeholder",
                     "-e", "TESTPILOT_MODEL_URL=http://model.invalid/v1",
                     "-e", "TEST_USERNAME=", "-e", "TEST_PASSWORD=", "-e", "TEST_TOKEN=",
                     "--tmpfs", "/audit-data:rw,size=256m",
                     "-v", str(ROOT / "src") + ":/app/src:ro",
                     "-v", str(ROOT / "tests") + ":/audit/tests:ro",
                      "-v", str(ROOT / "scripts") + ":/audit/scripts:ro",
                     "-v", str(ROOT / "deploy/package") + ":/audit/deploy/package:ro",
                      "-v", str(ROOT / "README.md") + ":/audit/README.md:ro",
                      "-v", str(ROOT / "LICENSE") + ":/audit/LICENSE:ro",
                      "-v", str(ROOT / "THIRD_PARTY_NOTICES.md") + ":/audit/THIRD_PARTY_NOTICES.md:ro",
                     "-v", "/var/run/docker.sock:/var/run/docker.sock",
                     "testpilot-runtime:0.1", "python", "-c", program]
        run(arguments, stdout=subprocess.DEVNULL)
        run(["docker", "network", "connect", "testpilot-targets", container_name])
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
                    database(f"REVOKE ALL PRIVILEGES ON `{database_name}`.* FROM 'testpilot'@'%';")
            finally:
                database(f"DROP DATABASE `{database_name}`;")


# 功能：运行完整后端检查及前端测试/构建。
# 输入：无参数；要求当前机器有 Docker、npm 和安装 .env。
# 输出/影响：调用隔离 backend 检查，再运行前端命令并打印结果；失败退出，不提交代码。
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
