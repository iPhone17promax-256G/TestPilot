#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
docker compose -f deploy/client.yaml --profile android stop
docker compose --env-file deploy/platform.env -f deploy/compose.yaml stop
