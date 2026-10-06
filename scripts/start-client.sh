#!/usr/bin/env bash
set -euo pipefail
AIQA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$AIQA_ROOT"
mkdir -p .client
chmod 700 .client
mkdir -p .client/apks
touch .client/execution.env
chmod 600 .client/execution.env
export AIQA_CLIENT_HOST_ROOT="$AIQA_ROOT/.client"
docker network inspect aiqa-entry >/dev/null 2>&1 || docker network create aiqa-entry >/dev/null
docker network inspect aiqa-targets >/dev/null 2>&1 || docker network create aiqa-targets >/dev/null
docker compose -f deploy/client.yaml up -d --no-build executor
if [[ "${1:-}" == "--android" ]]; then
  if [[ ! -c /dev/kvm ]]; then
    printf 'Android emulator requires /dev/kvm; configure an external Appium device instead.\n' >&2
    exit 1
  fi
  docker volume inspect aiqa_android >/dev/null 2>&1 || docker volume create aiqa_android >/dev/null
  docker compose -f deploy/client.yaml --profile android up -d --no-build appium
fi
docker compose -f deploy/client.yaml logs --tail 8 executor
printf 'Local executor: http://127.0.0.1:8021 · pair it on /environment\n'
