#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
[[ -f images.tar.gz ]] || { printf '当前包没有 images.tar.gz；请获取离线包或在线拉取成品镜像。\n' >&2; exit 1; }
sha256sum -c images.sha256
docker image load -i images.tar.gz
