#!/bin/bash

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
DEV_SCRIPT="${DEV_SCRIPT:-$ROOT/dev.sh}"

PATH_PREFIXES=(
  "$HOME/.local/bin"
  "$HOME/.volta/bin"
  "$HOME/.npm-global/bin"
  "$HOME/.cargo/bin"
  "/opt/homebrew/bin"
  "/usr/local/bin"
)

for dir in "${PATH_PREFIXES[@]}"; do
  if [ -d "$dir" ]; then
    export PATH="$dir:$PATH"
  fi
done

if [ ! -x "$DEV_SCRIPT" ]; then
  echo "未找到可执行的启动脚本: $DEV_SCRIPT"
  read -r -p "按回车关闭窗口..."
  exit 1
fi

export BIND_HOST="${BIND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-3011}"
BACKEND_PORT="${BACKEND_PORT:-3018}"
FRONTEND_URL="http://$BIND_HOST:$FRONTEND_PORT/"
AUTO_OPEN_TIMEOUT="${AUTO_OPEN_TIMEOUT:-60}"
AUTO_OPEN_INTERVAL="${AUTO_OPEN_INTERVAL:-1}"
WATCHER_PID=""
CURL_CMD="${CURL_CMD:-curl}"
OPEN_CMD="${OPEN_CMD:-open}"

cleanup_watcher() {
  if [ -n "${WATCHER_PID:-}" ]; then
    kill "$WATCHER_PID" 2>/dev/null || true
  fi
}

has_launcher_command() {
  local cmd="$1"
  [ -x "$cmd" ] || command -v "$cmd" >/dev/null 2>&1
}

maybe_auto_open_browser() {
  if [ "${AUTO_OPEN_BROWSER:-1}" = "0" ]; then
    return 0
  fi
  if ! has_launcher_command "$CURL_CMD" || ! has_launcher_command "$OPEN_CMD"; then
    return 0
  fi

  (
    deadline=$((SECONDS + AUTO_OPEN_TIMEOUT))
    while [ "$SECONDS" -lt "$deadline" ]; do
      if "$CURL_CMD" --silent --fail "$FRONTEND_URL" >/dev/null 2>&1; then
        "$OPEN_CMD" "$FRONTEND_URL" >/dev/null 2>&1 || true
        exit 0
      fi
      sleep "$AUTO_OPEN_INTERVAL"
    done
  ) &
  WATCHER_PID=$!
}

trap cleanup_watcher EXIT INT TERM

echo "正在启动 tickflow-stock-panel..."
echo "项目目录: $ROOT"
echo "前端地址: $FRONTEND_URL"
echo "后端地址: http://$BIND_HOST:$BACKEND_PORT"
echo

maybe_auto_open_browser

if ! bash "$DEV_SCRIPT"; then
  status=$?
  if [ "$status" -eq 0 ]; then
    status=1
  fi
  echo
  echo "启动失败，退出码: $status"
  read -r -p "按回车关闭窗口..."
  exit "$status"
fi
