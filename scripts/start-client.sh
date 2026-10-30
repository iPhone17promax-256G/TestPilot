#!/usr/bin/env bash
set -euo pipefail
TESTPILOT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TESTPILOT_ROOT"
mkdir -p .client
chmod 700 .client
mkdir -p .client/apks
touch .client/execution.env
chmod 600 .client/execution.env
export TESTPILOT_CLIENT_HOST_ROOT="$TESTPILOT_ROOT/.client"
export TESTPILOT_TLS_DIR="$(realpath -m "${TESTPILOT_TLS_DIR:-$TESTPILOT_ROOT/.tls}")"
case "$TESTPILOT_TLS_DIR/" in
  "$TESTPILOT_ROOT/.client/"*|"$TESTPILOT_ROOT/.runtime/"*)
    printf '证书目录须位于 .client/.runtime 之外，避免 CA 私钥被整体挂载。\n' >&2; exit 1 ;;
esac
mkdir -p "$TESTPILOT_TLS_DIR"
chmod 700 "$TESTPILOT_TLS_DIR"
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$TESTPILOT_ROOT/scripts/setup_https.py:/setup_https.py:ro" -v "$TESTPILOT_TLS_DIR:/tls" \
  testpilot-runtime:0.1 python /setup_https.py --output /tls
docker network inspect testpilot-entry >/dev/null 2>&1 || docker network create testpilot-entry >/dev/null
docker network inspect testpilot-targets >/dev/null 2>&1 || docker network create testpilot-targets >/dev/null
docker compose -f deploy/client.yaml up -d --no-build executor
if [[ "${1:-}" == "--android" ]]; then
  if [[ ! -c /dev/kvm ]]; then
    printf 'Android emulator requires /dev/kvm; configure an external Appium device instead.\n' >&2
    exit 1
  fi
  docker volume inspect testpilot_android >/dev/null 2>&1 || docker volume create testpilot_android >/dev/null
  docker compose -f deploy/client.yaml --profile android up -d --no-build appium
fi
docker compose -f deploy/client.yaml logs --tail 8 executor
printf 'Local executor: https://127.0.0.1:8021 · pair it on /environment\n'
printf '首次使用请在浏览器所在系统信任 %s/rootCA.pem；远程私有平台的 CA 也须加入客户端信任。\n' "$TESTPILOT_TLS_DIR"
