#!/usr/bin/env python3
"""发布者构建成品包。只复制安装文件；绝不打包 .env、用户资料和模型密钥。"""
import argparse
import hashlib
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
INSTALL_FILES=('compose.yaml','compose.android.yaml','generate_config.py','import-images.sh','install.sh','install.ps1','images.conf')
PUBLIC_FILES=('LICENSE','THIRD_PARTY_NOTICES.md')


def image_defaults():
    """读取安装器共用的镜像清单；返回固定组件到镜像的映射，不执行配置内容。"""
    result={}
    for line in (ROOT/'deploy/package/images.conf').read_text().splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        key,separator,value=line.partition('=')
        if not separator or key in result or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:@-]*',value):
            raise SystemExit('安装镜像清单格式不合法')
        result[key]=value
    if set(result)!={'runtime','mysql','redis','qdrant','performance','android'}:
        raise SystemExit('安装镜像清单缺少或包含未知组件')
    return result


def required_images(defaults,performance=False,android=False):
    """返回本包必须包含的镜像，基础服务始终含 Redis 和 Qdrant，可选组件按开关增加。"""
    keys=['runtime','mysql','redis','qdrant']
    if performance:keys.append('performance')
    if android:keys.append('android')
    return [defaults[key] for key in keys]


# 功能：构建可安装的发布包，可选附带离线镜像。
# 输入：output 是 Path 输出目录；offline 控制镜像打包，performance/android 选择可选镜像，refresh 允许安全范围内更新成品目录。
# 输出/影响：执行前端/Docker 构建，复制安装文件及说明，写 tar.gz/可选镜像与摘要；不复制用户数据或 .env，检查避免覆盖已安装目录。
def build(output,offline=False,performance=False,android=False,refresh=False):
    output=output.resolve()
    if output.exists() and not refresh:raise SystemExit('输出目录已存在；请选择新目录或显式 --refresh 成品目录')
    if output.exists() and (not output.is_relative_to(ROOT/'releases') or any((output/name).exists() for name in ['install.env','api.env','executor.env','data','client'])):
        raise SystemExit('不能覆盖已安装的目录或发布目录外的文件')
    if output.is_relative_to(ROOT/'deploy') or output.is_relative_to(ROOT/'src'):raise SystemExit('输出目录不能放进源码或部署模板')
    defaults=image_defaults()
    images=required_images(defaults,performance,android)
    subprocess.run(['npm','run','build'],cwd=ROOT/'web',check=True)
    subprocess.run(['docker','build','-t',defaults['runtime'],'-f','deploy/release.Dockerfile','.'],cwd=ROOT,check=True)
    output.mkdir(parents=True,exist_ok=True)
    # 只复制确定的安装模板，避免在模板目录运行过安装器后把 api.env 等私有文件带入发布包。
    for name in INSTALL_FILES:shutil.copy2(ROOT/'deploy/package'/name,output/name)
    for name in PUBLIC_FILES:shutil.copy2(ROOT/name,output/name)
    shutil.copy2(ROOT/'README.md',output/'使用说明.md')
    if offline:
        # 流式压缩，避免同时保留一份巨大的未压缩镜像 tar。
        with (output/'images.tar.gz').open('wb') as handle:
            save=subprocess.Popen(['docker','image','save',*images],stdout=subprocess.PIPE)
            compressed=subprocess.run(['gzip','-1'],stdin=save.stdout,stdout=handle)
            save.stdout.close()
            if save.wait() or compressed.returncode:raise SystemExit('镜像导出失败')
        hasher=hashlib.sha256()
        with (output/'images.tar.gz').open('rb') as handle:
            for chunk in iter(lambda:handle.read(1024*1024),b''):hasher.update(chunk)
        digest=hasher.hexdigest()
        (output/'images.sha256').write_text(digest+'  images.tar.gz\n')
    # refresh 目录可能残留其他文件；归档同样使用白名单，不把整个输出目录递归加入。
    packaged_files=[*INSTALL_FILES,*PUBLIC_FILES,'使用说明.md']
    if offline:packaged_files.extend(['images.tar.gz','images.sha256'])
    with tarfile.open(str(output)+'.tar.gz','w:gz',compresslevel=1) as archive:
        archive.add(output,arcname='TestPilot',recursive=False)
        for name in packaged_files:archive.add(output/name,arcname='TestPilot/'+name)
    print('成品包：'+str(output)+'.tar.gz；包含镜像：'+str(offline))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'releases/TestPilot-0.2')
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--performance',action='store_true')
    parser.add_argument('--android',action='store_true')
    parser.add_argument('--refresh',action='store_true')
    arguments=parser.parse_args()
    build(arguments.output,arguments.offline,arguments.performance,arguments.android,arguments.refresh)
