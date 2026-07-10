#!/usr/bin/env bash
# tickflow-stock-panel — 一键启动前后端
#
# 用法:
#   ./dev.sh                          # 默认 backend:3018  frontend:3011
#   BACKEND_PORT=8000 ./dev.sh        # 改后端端口
#   FRONTEND_PORT=5173 ./dev.sh       # 改前端端口
#
# Ctrl-C 同时关闭两端。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
BACKEND_PORT="${BACKEND_PORT:-3018}"
FRONTEND_PORT="${FRONTEND_PORT:-3011}"
BIND_HOST="${BIND_HOST:-0.0.0.0}"
LOCAL_UV="$ROOT/.uvvenv/bin/uv"
UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.cache/uv}"
VITE_BIN="$FRONTEND_DIR/node_modules/.bin/vite"

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

if command -v uv >/dev/null 2>&1; then
  UV_CMD="$(command -v uv)"
elif [ -x "$LOCAL_UV" ]; then
  UV_CMD="$LOCAL_UV"
else
  err "uv 未安装"
  echo "       安装方式:curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "       或在项目目录执行: python3.11 -m venv .uvvenv && ./.uvvenv/bin/pip install uv"
  exit 1
fi

mkdir -p "$UV_CACHE_DIR"

if command -v pnpm >/dev/null 2>&1; then
  PNPM_CMD=(pnpm)
elif command -v corepack >/dev/null 2>&1; then
  PNPM_CMD=(corepack pnpm)
elif command -v npm >/dev/null 2>&1; then
  PNPM_CMD=(npm exec --yes --package pnpm@9.10.0 -- pnpm)
else
  PNPM_CMD=()
fi

# ===== 2. 端口占用检查 —— 占用就直接 kill =====
free_port() {
  local name="$1" port="$2"
  local pids
  pids=$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
  if [ -z "$pids" ]; then
    return 0
  fi
  warn "端口 $port($name)被占用,kill 现有进程 PID: $(echo "$pids" | xargs)"
  # 先 TERM
  echo "$pids" | xargs kill 2>/dev/null || true
  sleep 1
  # 还活着就 KILL
  pids=$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$pids" ]; then
    warn "TERM 没杀掉,改用 KILL -9"
    echo "$pids" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
  # 再确认一次
  pids=$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$pids" ]; then
    err "端口 $port 仍被占用 — kill 失败。请手动处理:lsof -i :$port"
    exit 1
  fi
  ok "端口 $port 已释放"
}
free_port backend  "$BACKEND_PORT"
free_port frontend "$FRONTEND_PORT"

# ===== 3. 首次依赖安装 =====
if [ ! -d "$BACKEND_DIR/.venv" ]; then
  info "后端首次启动 — 安装 Python 依赖(约 1-2 分钟)..."
  ( cd "$BACKEND_DIR" && UV_CACHE_DIR="$UV_CACHE_DIR" "$UV_CMD" sync )
  ok "后端依赖装好了"
fi

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  if [ "${#PNPM_CMD[@]}" -eq 0 ]; then
    err "pnpm 未安装"
    echo "       安装方式:npm i -g pnpm   或   corepack enable && corepack prepare pnpm@9 --activate"
    exit 1
  fi
  info "前端首次启动 — 安装 Node 依赖..."
  ( cd "$FRONTEND_DIR" && CI=true "${PNPM_CMD[@]}" install )
  ok "前端依赖装好了"
fi

if [ ! -x "$VITE_BIN" ]; then
  err "前端 vite 可执行文件不存在,请先执行 pnpm install"
  exit 1
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
  # 等子进程退出,避免孤儿
  wait 2>/dev/null || true
  ok "已退出"
  exit 0
}
trap cleanup INT TERM

# 用 awk 加前缀(macOS sed 没有 -u line-buffered,改用 awk + fflush 兼容)
prefix_awk() {
  awk -v p="$1" '{ print p $0; fflush() }'
}

echo
echo -e "${BLUE}╭──────────────────────────────────────────────╮${NC}"
echo -e "${BLUE}│${NC}  ${GREEN}tickflow-stock-panel${NC}                        ${BLUE}│${NC}"
echo -e "${BLUE}│${NC}                                              ${BLUE}│${NC}"
echo -e "${BLUE}│${NC}  backend   ${YELLOW}http://$BIND_HOST:$BACKEND_PORT${NC}         ${BLUE}│${NC}"
echo -e "${BLUE}│${NC}  frontend  ${YELLOW}http://$BIND_HOST:$FRONTEND_PORT${NC}         ${BLUE}│${NC}"
echo -e "${BLUE}│${NC}                                              ${BLUE}│${NC}"
echo -e "${BLUE}│${NC}  Ctrl-C 同时关闭两端                          ${BLUE}│${NC}"
echo -e "${BLUE}╰──────────────────────────────────────────────╯${NC}"
echo

(
  cd "$BACKEND_DIR"
  UV_CACHE_DIR="$UV_CACHE_DIR" "$UV_CMD" run uvicorn app.main:app --reload --host "$BIND_HOST" --port "$BACKEND_PORT" 2>&1 \
    | prefix_awk "$(printf "${BLUE}[backend ]${NC} ")"
) &
PIDS+=("$!")

(
  cd "$FRONTEND_DIR"
  "$VITE_BIN" --host "$BIND_HOST" --port "$FRONTEND_PORT" 2>&1 \
    | prefix_awk "$(printf "${GREEN}[frontend]${NC} ")"
) &
PIDS+=("$!")

# 等任一退出(bash 4.3+)或全部退出(老 bash)
if wait -n 2>/dev/null; then
  warn "其中一个进程退出,正在关闭另一个..."
  cleanup
else
  # 老 bash 没有 wait -n,退化为 wait 全部
  wait
fi
