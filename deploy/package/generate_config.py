"""在成品镜像的 Python 内生成安装配置；宿主机无需 Python/Node 或项目源码。"""
import os
import re
import secrets
from pathlib import Path

root=Path(os.environ.get('AIQA_CONFIG_ROOT','/install'))
mode=os.environ['AIQA_INSTALL_MODE']
port=int(os.environ.get('AIQA_INSTALL_PORT','8020'))
name=os.environ.get('AIQA_INSTALL_NAME','aiqa-workspace')
if mode not in {'local','server'} or not 1024<=port<=65535 or not re.fullmatch(r'[a-z][a-z0-9_-]{0,40}',name):
    raise SystemExit('部署模式、端口或安装名称不合法')
target=root/'install.env'
if target.exists():
    # 不允许一次启动意外把服务器改成免登录。模式切换须显式重新安装到新目录。
    values=dict(line.split('=',1) for line in target.read_text().splitlines() if '=' in line)
    if values['AIQA_DEPLOYMENT_MODE']!=mode:raise SystemExit('已有安装模式不一致，请使用原模式或新的安装目录')
    print('沿用已有安装配置，保留数据库与密钥。')
else:
    image=os.environ.get('AIQA_INSTALL_IMAGE','aiqa-runtime:0.2')
    host_path=os.environ['AIQA_INSTALL_HOST_PATH']
    # compose env 值不能含换行/$；安装路径中的空格和中文可以保留。
    if any(c in host_path+image for c in '\r\n$'):raise SystemExit('安装路径或镜像名含不支持的字符')
    password=secrets.token_hex(24)
    values=dict(COMPOSE_PROJECT_NAME=name,AIQA_DEPLOYMENT_MODE=mode,AIQA_BIND='127.0.0.1' if mode=='local' else '0.0.0.0',
        AIQA_PORT=str(port),AIQA_RELEASE_IMAGE=image,AIQA_INSTALL_PATH=host_path,AIQA_TARGET_NETWORK=name+'-targets',
        AIQA_DB_PASSWORD=password,AIQA_DB_ROOT_PASSWORD=secrets.token_hex(32),
        AIQA_PERFORMANCE_IMAGE=os.environ.get('AIQA_INSTALL_PERFORMANCE_IMAGE','aiqa-jmeter:0.1'),
        AIQA_APPIUM_IMAGE=os.environ.get('AIQA_INSTALL_APPIUM_IMAGE','aiqa-appium:0.1'))
    common=dict(AIQA_DEPLOYMENT_MODE=mode,AIQA_MANAGED_EXECUTOR='true',AIQA_MANAGED_TOKEN=secrets.token_urlsafe(48))
    runtime=dict(common,AIQA_RUNNER_IMAGE=image,AIQA_PERFORMANCE_IMAGE=values['AIQA_PERFORMANCE_IMAGE'],
        AIQA_APPIUM_IMAGE=values['AIQA_APPIUM_IMAGE'],AIQA_APPIUM_CONTAINER=name+'-appium-1',AIQA_TARGET_NETWORK=values['AIQA_TARGET_NETWORK'])
    for path,data in [(target,values),(root/'api.env',dict(common,AIQA_DATABASE_URL=f'mysql+pymysql://aiqa:{password}@mysql:3306/aiqa?charset=utf8mb4')),(root/'executor.env',runtime),(root/'test.env',dict(TEST_USERNAME='',TEST_PASSWORD='',TEST_TOKEN=''))]:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as handle:handle.write(''.join(f'{key}={value}\n' for key,value in data.items()))
    for directory in ['data','client','client/apks']:
        (root/directory).mkdir(parents=True,exist_ok=True,mode=0o700)
    print('已生成私有安装配置。模式：'+mode+'；端口：'+str(port))
