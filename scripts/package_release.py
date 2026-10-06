#!/usr/bin/env python3
"""发布者构建成品包。只复制安装文件；绝不打包 .env、用户资料和模型密钥。"""
import argparse
import hashlib
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def build(output,offline=False,performance=False,android=False,refresh=False):
    output=output.resolve()
    if output.exists() and not refresh:raise SystemExit('输出目录已存在；请选择新目录或显式 --refresh 成品目录')
    if output.exists() and (not output.is_relative_to(ROOT/'releases') or any((output/name).exists() for name in ['install.env','api.env','executor.env','data','client'])):
        raise SystemExit('不能覆盖已安装的目录或发布目录外的文件')
    if output.is_relative_to(ROOT/'deploy') or output.is_relative_to(ROOT/'src'):raise SystemExit('输出目录不能放进源码或部署模板')
    subprocess.run(['npm','run','build'],cwd=ROOT/'web',check=True)
    subprocess.run(['docker','build','-t','aiqa-runtime:0.2','-f','deploy/release.Dockerfile','.'],cwd=ROOT,check=True)
    output.mkdir(parents=True,exist_ok=True)
    for path in (ROOT/'deploy/package').iterdir():
        if path.is_file():shutil.copy2(path,output/path.name)
    # 个人讲解文档只在本地保留；从公开仓库构建时，以公开 README 作为安装说明。
    guide=ROOT/'docs/双模式部署与安装.md'
    shutil.copy2(guide if guide.is_file() else ROOT/'README.md',output/'使用说明.md')
    images=['aiqa-runtime:0.2','mysql:8.4']
    if performance:images.append('aiqa-jmeter:0.1')
    if android:images.append('aiqa-appium:0.1')
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
    with tarfile.open(str(output)+'.tar.gz','w:gz',compresslevel=1) as archive:archive.add(output,arcname='AIQA')
    print('成品包：'+str(output)+'.tar.gz；包含镜像：'+str(offline))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'releases/AIQA-0.2')
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--performance',action='store_true')
    parser.add_argument('--android',action='store_true')
    parser.add_argument('--refresh',action='store_true')
    arguments=parser.parse_args()
    build(arguments.output,arguments.offline,arguments.performance,arguments.android,arguments.refresh)
