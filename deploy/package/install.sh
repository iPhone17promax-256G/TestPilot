#!/usr/bin/env bash
# 无需源代码、Node/npm 或宿主 Python。Windows 从 install.ps1 进入 WSL。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# 与发布脚本共用镜像清单；仅解析固定组件名和镜像值，不 source 文件或执行其中的命令。
declare -A TESTPILOT_DEFAULT_IMAGES=()
while IFS='=' read -r TESTPILOT_COMPONENT TESTPILOT_COMPONENT_IMAGE || [[ -n "${TESTPILOT_COMPONENT:-}" ]]; do
  [[ -z "$TESTPILOT_COMPONENT" || "$TESTPILOT_COMPONENT" == \#* ]] && continue
  case "$TESTPILOT_COMPONENT" in runtime|mysql|redis|qdrant|performance|android) ;;
    *) printf '安装镜像清单含未知组件：%s\n' "$TESTPILOT_COMPONENT" >&2; exit 1;; esac
  [[ "$TESTPILOT_COMPONENT_IMAGE" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]*$ && -z "${TESTPILOT_DEFAULT_IMAGES[$TESTPILOT_COMPONENT]:-}" ]] || { printf '安装镜像清单格式不合法。\n' >&2; exit 1; }
  TESTPILOT_DEFAULT_IMAGES[$TESTPILOT_COMPONENT]="$TESTPILOT_COMPONENT_IMAGE"
done < images.conf
for TESTPILOT_COMPONENT in runtime mysql redis qdrant performance android; do
  [[ -n "${TESTPILOT_DEFAULT_IMAGES[$TESTPILOT_COMPONENT]:-}" ]] || { printf '安装镜像清单缺少组件：%s\n' "$TESTPILOT_COMPONENT" >&2; exit 1; }
done
TESTPILOT_MODE=local
TESTPILOT_MODE_GIVEN=false
TESTPILOT_PORT=8020
TESTPILOT_NAME=testpilot-workspace
TESTPILOT_IMAGE="${TESTPILOT_DEFAULT_IMAGES[runtime]}"
TESTPILOT_PERFORMANCE_IMAGE="${TESTPILOT_DEFAULT_IMAGES[performance]}"
TESTPILOT_APPIUM_IMAGE="${TESTPILOT_DEFAULT_IMAGES[android]}"
TESTPILOT_OFFLINE=false
TESTPILOT_REPAIR=false
TESTPILOT_PERF=false
TESTPILOT_ANDROID=false
TESTPILOT_TLS_HOSTS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) TESTPILOT_MODE="$2"; TESTPILOT_MODE_GIVEN=true; shift 2;;
    --port) TESTPILOT_PORT="$2"; shift 2;;
    --name) TESTPILOT_NAME="$2"; shift 2;;
    --image) TESTPILOT_IMAGE="$2"; shift 2;;
    --performance-image) TESTPILOT_PERFORMANCE_IMAGE="$2"; shift 2;;
    --appium-image) TESTPILOT_APPIUM_IMAGE="$2"; shift 2;;
    --offline) TESTPILOT_OFFLINE=true; shift;;
    --repair) TESTPILOT_REPAIR=true; shift;;
    --performance) TESTPILOT_PERF=true; shift;;
    --android) TESTPILOT_ANDROID=true; shift;;
    --host) TESTPILOT_TLS_HOSTS+=(--host "$2"); shift 2;;
    *) printf '未知参数：%s\n' "$1" >&2; exit 2;;
  esac
done
command -v docker >/dev/null || { printf '请先安装 Docker： https://docs.docker.com/engine/install/\n' >&2; exit 1; }
docker info >/dev/null 2>&1 || { printf 'Docker 未启动或当前账号无法连接 Docker。\n' >&2; exit 1; }
docker compose version >/dev/null || { printf '请安装 Docker Compose 插件。\n' >&2; exit 1; }
TESTPILOT_FREE_KB="$(df -Pk . | awk 'NR==2 {print $4}')"
[[ "$TESTPILOT_FREE_KB" -gt 2097152 ]] || { printf '安装目录至少需要 2 GB 可用空间；镜像与 Android 另占空间。\n' >&2; exit 1; }
if [[ -f install.env ]]; then
  # 仅提取程序自己生成的非凭据字段，不 source 任意环境文件。
  TESTPILOT_SAVED_MODE="$(sed -n 's/^TESTPILOT_DEPLOYMENT_MODE=//p' install.env)"
  if $TESTPILOT_MODE_GIVEN && [[ "$TESTPILOT_MODE" != "$TESTPILOT_SAVED_MODE" ]]; then printf '已有安装不能通过启动参数改变认证模式，请使用新目录。\n' >&2; exit 1; fi
  TESTPILOT_MODE="$TESTPILOT_SAVED_MODE"
  TESTPILOT_IMAGE="$(sed -n 's/^TESTPILOT_RELEASE_IMAGE=//p' install.env)"
  TESTPILOT_SAVED_PERFORMANCE_IMAGE="$(sed -n 's/^TESTPILOT_PERFORMANCE_IMAGE=//p' install.env)"
  TESTPILOT_SAVED_APPIUM_IMAGE="$(sed -n 's/^TESTPILOT_APPIUM_IMAGE=//p' install.env)"
  TESTPILOT_PERFORMANCE_IMAGE="${TESTPILOT_SAVED_PERFORMANCE_IMAGE:-$TESTPILOT_PERFORMANCE_IMAGE}"
  TESTPILOT_APPIUM_IMAGE="${TESTPILOT_SAVED_APPIUM_IMAGE:-$TESTPILOT_APPIUM_IMAGE}"
fi
if $TESTPILOT_OFFLINE; then bash import-images.sh; fi
TESTPILOT_REQUIRED_IMAGES=("$TESTPILOT_IMAGE" "${TESTPILOT_DEFAULT_IMAGES[mysql]}" "${TESTPILOT_DEFAULT_IMAGES[redis]}" "${TESTPILOT_DEFAULT_IMAGES[qdrant]}")
if $TESTPILOT_PERF; then TESTPILOT_REQUIRED_IMAGES+=("$TESTPILOT_PERFORMANCE_IMAGE"); fi
if $TESTPILOT_ANDROID; then TESTPILOT_REQUIRED_IMAGES+=("$TESTPILOT_APPIUM_IMAGE"); fi
for TESTPILOT_REQUIRED_IMAGE in "${TESTPILOT_REQUIRED_IMAGES[@]}"; do
  if ! docker image inspect "$TESTPILOT_REQUIRED_IMAGE" >/dev/null 2>&1; then
    if $TESTPILOT_OFFLINE; then printf '离线包缺少镜像：%s\n' "$TESTPILOT_REQUIRED_IMAGE" >&2; exit 1; fi
    docker pull "$TESTPILOT_REQUIRED_IMAGE"
  fi
done
docker run --rm --pull=never --network none --user "$(id -u):$(id -g)" -v "$PWD:/install" \
  -e TESTPILOT_INSTALL_MODE="$TESTPILOT_MODE" -e TESTPILOT_INSTALL_PORT="$TESTPILOT_PORT" -e TESTPILOT_INSTALL_NAME="$TESTPILOT_NAME" \
  -e TESTPILOT_INSTALL_HOST_PATH="$PWD" -e TESTPILOT_INSTALL_IMAGE="$TESTPILOT_IMAGE" \
  -e TESTPILOT_INSTALL_PERFORMANCE_IMAGE="$TESTPILOT_PERFORMANCE_IMAGE" -e TESTPILOT_INSTALL_APPIUM_IMAGE="$TESTPILOT_APPIUM_IMAGE" \
  "$TESTPILOT_IMAGE" python /install/generate_config.py
TESTPILOT_TLS_DIR="$(sed -n 's/^TESTPILOT_TLS_DIR=//p' install.env)"
[[ -n "$TESTPILOT_TLS_DIR" ]] || { printf '安装配置缺少 HTTPS 证书目录。\n' >&2; exit 1; }
mkdir -p "$TESTPILOT_TLS_DIR"
chmod 700 "$TESTPILOT_TLS_DIR"
docker run --rm --pull=never --network none --user "$(id -u):$(id -g)" -v "$TESTPILOT_TLS_DIR:/tls" \
  "$TESTPILOT_IMAGE" python /app/scripts/setup_https.py --output /tls "${TESTPILOT_TLS_HOSTS[@]}"
TESTPILOT_COMPOSE=(docker compose --env-file install.env -f compose.yaml)
TESTPILOT_PULL_OPTIONS=()
if $TESTPILOT_OFFLINE; then TESTPILOT_PULL_OPTIONS=(--pull never); fi
if $TESTPILOT_REPAIR; then "${TESTPILOT_COMPOSE[@]}" up -d --no-build "${TESTPILOT_PULL_OPTIONS[@]}" --force-recreate api executor; else "${TESTPILOT_COMPOSE[@]}" up -d --no-build "${TESTPILOT_PULL_OPTIONS[@]}" mysql api executor; fi
if $TESTPILOT_ANDROID; then
  [[ -c /dev/kvm ]] || { printf '基础服务已启动；模拟器需要 /dev/kvm。可改用外部 Appium。\n' >&2; exit 1; }
  "${TESTPILOT_COMPOSE[@]}" -f compose.android.yaml --profile android up -d --no-build "${TESTPILOT_PULL_OPTIONS[@]}" executor appium
fi
TESTPILOT_READY=false
for TESTPILOT_ATTEMPT in {1..60}; do
  if "${TESTPILOT_COMPOSE[@]}" exec -T api python -c "import httpx; httpx.get('https://127.0.0.1:8020/api/health').raise_for_status()" >/dev/null 2>&1; then TESTPILOT_READY=true; break; fi
  sleep 1
done
$TESTPILOT_READY || { printf '启动未就绪，请查看 compose logs api executor。\n' >&2; exit 1; }
TESTPILOT_PORT="$(sed -n 's/^TESTPILOT_PORT=//p' install.env)"
printf 'TestPilot 已启动 (%s)： https://127.0.0.1:%s\n' "$TESTPILOT_MODE" "$TESTPILOT_PORT"
printf '首次访问请在客户端系统信任 %s/rootCA.pem；不要分享 rootCA-key.pem。\n' "$TESTPILOT_TLS_DIR"
if [[ "$TESTPILOT_MODE" == server ]]; then printf '内网访问须使用证书覆盖的域名或 IP，并在客户端信任签发 CA；首次打开创建管理员。\n'; fi
