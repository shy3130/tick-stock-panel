#!/usr/bin/env bash
# tickflow-stock-panel — 后端一键启动
#
# 用法:
#   ./dev.sh                          # 默认 backend:3018
#   BACKEND_PORT=8000 ./dev.sh        # 改后端端口
#
# Ctrl-C 关闭后端。
# 说明：本脚本只启动后端 API。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT/backend"
BACKEND_PORT="${BACKEND_PORT:-3018}"

# Match Docker's BACKEND_EXTRAS behavior so old CPUs can select Polars'
# rtcompat runtime before the backend starts. An exported value wins over .env.
if [[ -z "${BACKEND_EXTRAS+x}" && -f "$ROOT/.env" ]]; then
  BACKEND_EXTRAS="$(awk '/^[[:space:]]*BACKEND_EXTRAS[[:space:]]*=/ {sub(/^[^=]*=/, ""); gsub(/^[[:space:]]+|[[:space:]]+$/, ""); print; exit}' "$ROOT/.env")"
fi
BACKEND_EXTRAS="${BACKEND_EXTRAS:-}"
BACKEND_EXTRA_ARGS=()
if [[ -n "$BACKEND_EXTRAS" ]]; then
  read -r -a backend_extras <<< "$BACKEND_EXTRAS"
  for extra in "${backend_extras[@]}"; do
    BACKEND_EXTRA_ARGS+=(--extra "$extra")
  done
fi

BLUE='\033[0;34m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
GRAY='\033[0;90m'
NC='\033[0m'

info()  { echo -e "${GRAY}[dev]${NC} $*"; }
ok()    { echo -e "${GREEN}[dev]${NC} $*"; }
warn()  { echo -e "${YELLOW}[dev]${NC} $*"; }
err()   { echo -e "${RED}[dev]${NC} $*" >&2; }

# ===== 1. 依赖检查 =====
require_cmd() {
  local cmd="$1" hint="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    err "$cmd 未安装"
    echo "       安装方式:$hint"
    exit 1
  fi
}
require_cmd uv   "curl -LsSf https://astral.sh/uv/install.sh | sh"

# ===== 2. 端口占用检查 —— 占用就直接 kill =====
free_port() {
  local name="$1" port="$2"
  local pids
  pids=$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
  if [ -z "$pids" ]; then
    return 0
  fi
  warn "端口 $port($name)被占用,kill 现有进程 PID: $(echo "$pids" | xargs)"
  echo "$pids" | xargs kill 2>/dev/null || true
  sleep 1
  pids=$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$pids" ]; then
    warn "TERM 没杀掉,改用 KILL -9"
    echo "$pids" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
  pids=$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$pids" ]; then
    err "端口 $port 仍被占用 — kill 失败。请手动处理:lsof -i :$port"
    exit 1
  fi
  ok "端口 $port 已释放"
}
free_port backend "$BACKEND_PORT"

# ===== 3. 依赖安装 =====
if [ ! -d "$BACKEND_DIR/.venv" ] || [ "${#BACKEND_EXTRA_ARGS[@]}" -gt 0 ]; then
  if [ "${#BACKEND_EXTRA_ARGS[@]}" -gt 0 ]; then
    info "同步后端 Python 依赖，extras: $BACKEND_EXTRAS"
  else
    info "后端首次启动 — 安装 Python 依赖(约 1-2 分钟)..."
  fi
  ( cd "$BACKEND_DIR" && uv sync "${BACKEND_EXTRA_ARGS[@]}" )
  ok "后端依赖装好了"
fi

# ===== 4. 启动 + 日志前缀 =====
PIDS=()

cleanup() {
  echo
  info "关闭服务..."
  for pid in "${PIDS[@]:-}"; do
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
  ok "已退出"
  exit 0
}
trap cleanup INT TERM

prefix_awk() {
  awk -v p="$1" '{ print p $0; fflush() }'
}

echo
echo -e "${BLUE}╭──────────────────────────────────────────────╮${NC}"
echo -e "${BLUE}│${NC}  ${GREEN}tickflow-stock-panel${NC} (backend)               ${BLUE}│${NC}"
echo -e "${BLUE}│${NC}                                              ${BLUE}│${NC}"
echo -e "${BLUE}│${NC}  backend   ${YELLOW}http://localhost:$BACKEND_PORT${NC}          ${BLUE}│${NC}"
echo -e "${BLUE}│${NC}                                              ${BLUE}│${NC}"
echo -e "${BLUE}│${NC}  Ctrl-C 关闭后端                              ${BLUE}│${NC}"
echo -e "${BLUE}╰──────────────────────────────────────────────╯${NC}"
echo

(
  cd "$BACKEND_DIR"
  uv run uvicorn app.main:app --reload --host 127.0.0.1 --port "$BACKEND_PORT" 2>&1 \
    | prefix_awk "$(printf "${BLUE}[backend ]${NC} ")"
) &
PIDS+=("$!")

wait
