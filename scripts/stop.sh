#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ ! -f deploy/platform.env ]]; then
  printf '尚未生成部署配置，无需停止服务。\n'
  exit 0
fi
# 两套 Compose 都需要同一份 TLS 路径；新终端和 CI 步骤不会继承启动脚本的局部变量。
# 一组停止失败时仍尝试停止另一组，最后保留失败状态供使用者和 CI 检查。
TESTPILOT_STOP_STATUS=0
docker compose --env-file deploy/platform.env -f deploy/client.yaml --profile android stop || TESTPILOT_STOP_STATUS=$?
docker compose --env-file deploy/platform.env -f deploy/compose.yaml stop || TESTPILOT_STOP_STATUS=$?
exit "$TESTPILOT_STOP_STATUS"
