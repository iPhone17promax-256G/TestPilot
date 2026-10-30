"""在成品镜像的 Python 内生成安装配置；宿主机无需 Python/Node 或项目源码。"""
import os
import re
import secrets
from pathlib import Path

root=Path(os.environ.get('TESTPILOT_CONFIG_ROOT','/install'))
mode=os.environ['TESTPILOT_INSTALL_MODE']
port=int(os.environ.get('TESTPILOT_INSTALL_PORT','8020'))
name=os.environ.get('TESTPILOT_INSTALL_NAME','testpilot-workspace')
host_path=os.environ['TESTPILOT_INSTALL_HOST_PATH']
tls_path=str(Path(host_path)/'tls')
if any(c in tls_path for c in '\r\n$'):raise SystemExit('证书路径含不支持的字符')
if mode not in {'local','server'} or not 1024<=port<=65535 or not re.fullmatch(r'[a-z][a-z0-9_-]{0,40}',name):
    raise SystemExit('部署模式、端口或安装名称不合法')
target=root/'install.env'
if target.exists():
    # 这里 mode 是安装时选择的 local/server 部署模式，和 Agent 工具审批无关；已有 server 安装不能被一次启动改成免登录，切换部署方式须显式安装到新目录。
    values=dict(line.split('=',1) for line in target.read_text().splitlines() if '=' in line)
    if values['TESTPILOT_DEPLOYMENT_MODE']!=mode:raise SystemExit('已有安装模式不一致，请使用原模式或新的安装目录')
    if not values.get('TESTPILOT_TLS_DIR'):
        fd=os.open(target,os.O_WRONLY|os.O_APPEND)
        with os.fdopen(fd,'w') as handle:handle.write('\nTESTPILOT_TLS_DIR='+tls_path+'\n')
    if not values.get('TESTPILOT_REDIS_PASSWORD'):
        queue_password=secrets.token_hex(24)
        # 升级只追加 Redis 配置，保留原数据库密码、账号数据和执行服务令牌。
        for path,line in [(target,f'TESTPILOT_REDIS_PASSWORD={queue_password}\n'),(root/'api.env',f'TESTPILOT_REDIS_URL=redis://:{queue_password}@redis:6379/0\n')]:
            fd=os.open(path,os.O_WRONLY|os.O_APPEND)
            with os.fdopen(fd,'w') as handle:handle.write('\n'+line)
    print('沿用已有安装配置，保留数据库与密钥。')
    if not values.get('TESTPILOT_QDRANT_API_KEY'):
        vector_key=secrets.token_hex(24)
        for path,line in [(target,'TESTPILOT_QDRANT_API_KEY='+vector_key+'\n'),(root/'api.env','TESTPILOT_QDRANT_URL=http://qdrant:6333\nTESTPILOT_QDRANT_API_KEY='+vector_key+'\n')]:
            fd=os.open(path,os.O_WRONLY|os.O_APPEND)
            with os.fdopen(fd,'w') as handle:handle.write('\n'+line)
else:
    image=os.environ.get('TESTPILOT_INSTALL_IMAGE','testpilot-runtime:0.2')
    # 写入 Compose 的 .env 值不能含换行或 $，避免一项配置拆成多行或被 Compose 当作变量展开；安装目录的空格和中文不属于这些禁止字符。
    if any(c in host_path+image for c in '\r\n$'):raise SystemExit('安装路径或镜像名含不支持的字符')
    password=secrets.token_hex(24)
    redis_password=secrets.token_hex(24)
    vector_key=secrets.token_hex(24)
    values=dict(COMPOSE_PROJECT_NAME=name,TESTPILOT_DEPLOYMENT_MODE=mode,TESTPILOT_BIND='127.0.0.1' if mode=='local' else '0.0.0.0',
        TESTPILOT_PORT=str(port),TESTPILOT_RELEASE_IMAGE=image,TESTPILOT_INSTALL_PATH=host_path,TESTPILOT_TLS_DIR=tls_path,TESTPILOT_TARGET_NETWORK=name+'-targets',
        TESTPILOT_DB_PASSWORD=password,TESTPILOT_DB_ROOT_PASSWORD=secrets.token_hex(32),TESTPILOT_REDIS_PASSWORD=redis_password,TESTPILOT_QDRANT_API_KEY=vector_key,
        TESTPILOT_PERFORMANCE_IMAGE=os.environ.get('TESTPILOT_INSTALL_PERFORMANCE_IMAGE','testpilot-jmeter:0.1'),
        TESTPILOT_APPIUM_IMAGE=os.environ.get('TESTPILOT_INSTALL_APPIUM_IMAGE','testpilot-appium:0.1'))
    common=dict(TESTPILOT_DEPLOYMENT_MODE=mode,TESTPILOT_MANAGED_EXECUTOR='true',TESTPILOT_MANAGED_TOKEN=secrets.token_urlsafe(48))
    runtime=dict(common,TESTPILOT_RUNNER_IMAGE=image,TESTPILOT_PERFORMANCE_IMAGE=values['TESTPILOT_PERFORMANCE_IMAGE'],
        TESTPILOT_APPIUM_IMAGE=values['TESTPILOT_APPIUM_IMAGE'],TESTPILOT_APPIUM_CONTAINER=name+'-appium-1',TESTPILOT_TARGET_NETWORK=values['TESTPILOT_TARGET_NETWORK'])
    for path,data in [(target,values),(root/'api.env',dict(common,TESTPILOT_QDRANT_URL='http://qdrant:6333',TESTPILOT_QDRANT_API_KEY=vector_key,TESTPILOT_REDIS_URL=f'redis://:{redis_password}@redis:6379/0',TESTPILOT_DATABASE_URL=f'mysql+pymysql://testpilot:{password}@mysql:3306/testpilot?charset=utf8mb4')),(root/'executor.env',runtime),(root/'test.env',dict(TEST_USERNAME='',TEST_PASSWORD='',TEST_TOKEN=''))]:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as handle:handle.write(''.join(f'{key}={value}\n' for key,value in data.items()))
    for directory in ['data','client','client/apks','tls']:
        (root/directory).mkdir(parents=True,exist_ok=True,mode=0o700)
    print('已生成私有安装配置。模式：'+mode+'；端口：'+str(port))
