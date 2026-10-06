#!/usr/bin/env bash
set -euo pipefail
AIQA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$AIQA_ROOT"
AIQA_REBUILD=false
AIQA_ANDROID=false
for AIQA_ARG in "$@"; do
  case "$AIQA_ARG" in
    --rebuild) AIQA_REBUILD=true ;;
    --local) export AIQA_DEPLOYMENT_MODE=local ;;
    --server) export AIQA_DEPLOYMENT_MODE=server ;;
    --android) AIQA_ANDROID=true ;;
    *) printf 'Usage: bash scripts/start.sh [--rebuild] [--android] [--local|--server]\n' >&2; exit 2 ;;
  esac
done
python3 scripts/setup.py
python3 scripts/platform_env.py
mkdir -p .runtime/apps
if [[ ! -d web/node_modules ]]; then (cd web && npm ci); fi
(cd web && npm run build)
if $AIQA_REBUILD || ! docker image inspect aiqa-runtime:0.1 >/dev/null 2>&1; then
  docker build -t aiqa-runtime:0.1 -f deploy/Dockerfile .
fi
if $AIQA_REBUILD || ! docker image inspect aiqa-jmeter:0.1 >/dev/null 2>&1; then
  docker build -t aiqa-jmeter:0.1 -f deploy/jmeter/Dockerfile .
fi
export AIQA_CLIENT_HOST_ROOT="$AIQA_ROOT/.client"
docker compose --env-file deploy/platform.env -f deploy/compose.yaml up -d --no-build
if docker inspect mm-test-app >/dev/null 2>&1; then
  AIQA_CONNECTED="$(docker inspect -f '{{if index .NetworkSettings.Networks "aiqa-targets"}}yes{{end}}' mm-test-app)"
  if [[ "$AIQA_CONNECTED" != "yes" ]]; then
    docker network connect --alias mm-target aiqa-targets mm-test-app
  fi
fi
AIQA_READY=false
for AIQA_ATTEMPT in {1..45}; do
  if docker exec aiqa-api-1 aiqa status >/dev/null 2>&1; then AIQA_READY=true; break; fi
  sleep 1
done
if ! $AIQA_READY; then
  printf 'AIQA did not become healthy; inspect docker compose logs.\n' >&2
  exit 1
fi
if $AIQA_ANDROID; then
  if [[ ! -c /dev/kvm ]]; then printf 'Android emulator requires WSL /dev/kvm; use an external Appium device instead.\n' >&2; exit 1; fi
  if $AIQA_REBUILD || ! docker image inspect aiqa-appium:0.1 >/dev/null 2>&1; then
    docker build -t aiqa-appium:0.1 -f deploy/appium/Dockerfile .
  fi
  # 新版执行服务已由主 Compose 自动启动；这里只启动 Android，不能再启动旧的配对网关。
  docker volume inspect aiqa_android >/dev/null 2>&1 || docker volume create aiqa_android >/dev/null
  docker compose -f deploy/client.yaml --profile android up -d --no-build appium
  python3 scripts/setup_android.py
fi
printf 'AIQA ready: http://127.0.0.1:8020\n'
