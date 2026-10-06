#!/usr/bin/env bash
# 无需源代码、Node/npm 或宿主 Python。Windows 从 install.ps1 进入 WSL。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
AIQA_MODE=local
AIQA_MODE_GIVEN=false
AIQA_PORT=8020
AIQA_NAME=aiqa-workspace
AIQA_IMAGE=aiqa-runtime:0.2
AIQA_PERFORMANCE_IMAGE=aiqa-jmeter:0.1
AIQA_APPIUM_IMAGE=aiqa-appium:0.1
AIQA_OFFLINE=false
AIQA_REPAIR=false
AIQA_PERF=false
AIQA_ANDROID=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) AIQA_MODE="$2"; AIQA_MODE_GIVEN=true; shift 2;;
    --port) AIQA_PORT="$2"; shift 2;;
    --name) AIQA_NAME="$2"; shift 2;;
    --image) AIQA_IMAGE="$2"; shift 2;;
    --performance-image) AIQA_PERFORMANCE_IMAGE="$2"; shift 2;;
    --appium-image) AIQA_APPIUM_IMAGE="$2"; shift 2;;
    --offline) AIQA_OFFLINE=true; shift;;
    --repair) AIQA_REPAIR=true; shift;;
    --performance) AIQA_PERF=true; shift;;
    --android) AIQA_ANDROID=true; shift;;
    *) printf '未知参数：%s\n' "$1" >&2; exit 2;;
  esac
done
command -v docker >/dev/null || { printf '请先安装 Docker： https://docs.docker.com/engine/install/\n' >&2; exit 1; }
docker info >/dev/null 2>&1 || { printf 'Docker 未启动或当前账号无法连接 Docker。\n' >&2; exit 1; }
docker compose version >/dev/null || { printf '请安装 Docker Compose 插件。\n' >&2; exit 1; }
AIQA_FREE_KB="$(df -Pk . | awk 'NR==2 {print $4}')"
[[ "$AIQA_FREE_KB" -gt 2097152 ]] || { printf '安装目录至少需要 2 GB 可用空间；镜像与 Android 另占空间。\n' >&2; exit 1; }
if [[ -f install.env ]]; then
  # 仅提取程序自己生成的非凭据字段，不 source 任意环境文件。
  AIQA_SAVED_MODE="$(sed -n 's/^AIQA_DEPLOYMENT_MODE=//p' install.env)"
  if $AIQA_MODE_GIVEN && [[ "$AIQA_MODE" != "$AIQA_SAVED_MODE" ]]; then printf '已有安装不能通过启动参数改变认证模式，请使用新目录。\n' >&2; exit 1; fi
  AIQA_MODE="$AIQA_SAVED_MODE"
  AIQA_IMAGE="$(sed -n 's/^AIQA_RELEASE_IMAGE=//p' install.env)"
fi
if $AIQA_OFFLINE; then bash import-images.sh; fi
for AIQA_REQUIRED_IMAGE in "$AIQA_IMAGE" mysql:8.4; do
  if ! docker image inspect "$AIQA_REQUIRED_IMAGE" >/dev/null 2>&1; then
    if $AIQA_OFFLINE; then printf '离线包缺少镜像：%s\n' "$AIQA_REQUIRED_IMAGE" >&2; exit 1; fi
    docker pull "$AIQA_REQUIRED_IMAGE"
  fi
done
docker run --rm --network none --user "$(id -u):$(id -g)" -v "$PWD:/install" \
  -e AIQA_INSTALL_MODE="$AIQA_MODE" -e AIQA_INSTALL_PORT="$AIQA_PORT" -e AIQA_INSTALL_NAME="$AIQA_NAME" \
  -e AIQA_INSTALL_HOST_PATH="$PWD" -e AIQA_INSTALL_IMAGE="$AIQA_IMAGE" \
  -e AIQA_INSTALL_PERFORMANCE_IMAGE="$AIQA_PERFORMANCE_IMAGE" -e AIQA_INSTALL_APPIUM_IMAGE="$AIQA_APPIUM_IMAGE" \
  "$AIQA_IMAGE" python /install/generate_config.py
AIQA_COMPOSE=(docker compose --env-file install.env -f compose.yaml)
if $AIQA_REPAIR; then "${AIQA_COMPOSE[@]}" up -d --no-build --force-recreate api executor; else "${AIQA_COMPOSE[@]}" up -d --no-build mysql api executor; fi
if $AIQA_PERF; then
  AIQA_PERF_IMAGE="$(sed -n 's/^AIQA_PERFORMANCE_IMAGE=//p' install.env)"
  docker image inspect "$AIQA_PERF_IMAGE" >/dev/null 2>&1 || docker pull "$AIQA_PERF_IMAGE"
fi
if $AIQA_ANDROID; then
  [[ -c /dev/kvm ]] || { printf '基础服务已启动；模拟器需要 /dev/kvm。可改用外部 Appium。\n' >&2; exit 1; }
  "${AIQA_COMPOSE[@]}" -f compose.android.yaml --profile android up -d --no-build executor appium
fi
AIQA_READY=false
for AIQA_ATTEMPT in {1..60}; do
  if "${AIQA_COMPOSE[@]}" exec -T api python -c "import httpx; httpx.get('http://127.0.0.1:8020/api/health').raise_for_status()" >/dev/null 2>&1; then AIQA_READY=true; break; fi
  sleep 1
done
$AIQA_READY || { printf '启动未就绪，请查看 compose logs api executor。\n' >&2; exit 1; }
AIQA_PORT="$(sed -n 's/^AIQA_PORT=//p' install.env)"
printf 'AIQA 已启动 (%s)： http://127.0.0.1:%s\n' "$AIQA_MODE" "$AIQA_PORT"
if [[ "$AIQA_MODE" == server ]]; then printf '内网客户端使用服务器 IP；首次打开创建管理员。生产环境请用 HTTPS 反向代理。\n'; fi
