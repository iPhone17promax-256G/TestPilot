#!/usr/bin/env bash
set -euo pipefail
TESTPILOT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TESTPILOT_ROOT"
TESTPILOT_REBUILD=false
TESTPILOT_ANDROID=false
for TESTPILOT_ARG in "$@"; do
  case "$TESTPILOT_ARG" in
    --rebuild) TESTPILOT_REBUILD=true ;;
    --local) export TESTPILOT_DEPLOYMENT_MODE=local ;;
    --server) export TESTPILOT_DEPLOYMENT_MODE=server ;;
    --android) TESTPILOT_ANDROID=true ;;
    *) printf 'Usage: bash scripts/start.sh [--rebuild] [--android] [--local|--server]\n' >&2; exit 2 ;;
  esac
done
python3 scripts/setup.py
python3 scripts/platform_env.py
mkdir -p .runtime/apps
if [[ ! -d web/node_modules ]]; then (cd web && npm ci); fi
(cd web && npm run build)
if $TESTPILOT_REBUILD || ! docker image inspect testpilot-runtime:0.1 >/dev/null 2>&1; then
  docker build -t testpilot-runtime:0.1 -f deploy/Dockerfile .
fi
TESTPILOT_TLS_DIR="$(sed -n 's/^TESTPILOT_TLS_DIR=//p' deploy/platform.env)"
[[ -n "$TESTPILOT_TLS_DIR" ]] || { printf '没有配置 HTTPS 证书目录。\n' >&2; exit 1; }
# 生成器只在此短生命周期、无网络容器中接触 CA 私钥；常驻服务仅挂载各自的叶证书。
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$TESTPILOT_ROOT/scripts/setup_https.py:/setup_https.py:ro" -v "$TESTPILOT_TLS_DIR:/tls" \
  testpilot-runtime:0.1 python /setup_https.py --output /tls
if $TESTPILOT_REBUILD || ! docker image inspect testpilot-jmeter:0.1 >/dev/null 2>&1; then
  docker build -t testpilot-jmeter:0.1 -f deploy/jmeter/Dockerfile .
fi
export TESTPILOT_CLIENT_HOST_ROOT="$TESTPILOT_ROOT/.client"
docker compose --env-file deploy/platform.env -f deploy/compose.yaml up -d --no-build
if docker inspect mm-test-app >/dev/null 2>&1; then
  TESTPILOT_CONNECTED="$(docker inspect -f '{{if index .NetworkSettings.Networks "testpilot-targets"}}yes{{end}}' mm-test-app)"
  if [[ "$TESTPILOT_CONNECTED" != "yes" ]]; then
    docker network connect --alias mm-target testpilot-targets mm-test-app
  fi
fi
TESTPILOT_READY=false
for TESTPILOT_ATTEMPT in {1..45}; do
  if docker exec testpilot-api-1 testpilot status >/dev/null 2>&1; then TESTPILOT_READY=true; break; fi
  sleep 1
done
if ! $TESTPILOT_READY; then
  printf 'TestPilot did not become healthy; inspect docker compose logs.\n' >&2
  exit 1
fi
if $TESTPILOT_ANDROID; then
  if [[ ! -c /dev/kvm ]]; then printf 'Android emulator requires WSL /dev/kvm; use an external Appium device instead.\n' >&2; exit 1; fi
  if $TESTPILOT_REBUILD || ! docker image inspect testpilot-appium:0.1 >/dev/null 2>&1; then
    docker build -t testpilot-appium:0.1 -f deploy/appium/Dockerfile .
  fi
  # 新版执行服务已由主 Compose 自动启动；这里只启动 Android，不能再启动旧的配对网关。
  docker volume inspect testpilot_android >/dev/null 2>&1 || docker volume create testpilot_android >/dev/null
  docker compose -f deploy/client.yaml --profile android up -d --no-build appium
  python3 scripts/setup_android.py
fi
printf 'TestPilot ready: https://127.0.0.1:8020\n'
printf '首次访问请在浏览器所在系统信任本次安装的本地 CA：%s/rootCA.pem；不要分享 rootCA-key.pem。\n' "$TESTPILOT_TLS_DIR"
