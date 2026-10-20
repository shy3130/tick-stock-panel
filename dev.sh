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

# Read only the launcher-owned keys from .env. Do not source the whole file:
# .env is data, not a shell script, and may contain values that are unsafe or
# invalid as Bash syntax. Exported environment variables keep highest priority.
read_dotenv_value() {
  local key="$1"
  if [[ ! -f "$ROOT/.env" ]]; then
    return 0
  fi
  awk -v wanted="$key" '
    $0 ~ "^[[:space:]]*" wanted "[[:space:]]*=" {
      sub(/^[^=]*=/, "")
      sub(/[[:space:]]+#.*$/, "")
      gsub(/^[[:space:]]+|[[:space:]]+$/, "")
      if (($0 ~ /^".*"$/) || ($0 ~ /^\047.*\047$/)) {
        $0 = substr($0, 2, length($0) - 2)
      }
      print
      exit
    }
  ' "$ROOT/.env"
}

ENV_HOST="$(read_dotenv_value HOST)"
ENV_PORT="$(read_dotenv_value PORT)"
BACKEND_HOST="${HOST:-${ENV_HOST:-0.0.0.0}}"
# Keep BACKEND_PORT as a backwards-compatible explicit override.
BACKEND_PORT="${BACKEND_PORT:-${PORT:-${ENV_PORT:-3018}}}"
FRONTEND_PORT="${FRONTEND_PORT:-3011}"
UVICORN_ENV_ARGS=()
if [[ -f "$ROOT/.env" ]]; then
  UVICORN_ENV_ARGS=(--env-file "$ROOT/.env")
fi
DISPLAY_HOST="$BACKEND_HOST"
if [[ "$DISPLAY_HOST" == "0.0.0.0" || "$DISPLAY_HOST" == "::" ]]; then
  DISPLAY_HOST="localhost"
fi

# Match Docker's BACKEND_EXTRAS behavior so old CPUs can select Polars'
# rtcompat runtime before the backend starts. An exported value wins over .env.
if [[ -z "${BACKEND_EXTRAS+x}" && -f "$ROOT/.env" ]]; then
  BACKEND_EXTRAS="$(read_dotenv_value BACKEND_EXTRAS)"
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
require_cmd pnpm "npm i -g pnpm   或   corepack enable && corepack prepare pnpm@9 --activate"

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
# 按本仓库 venv 的绝对路径识别整组后端进程。
# uvicorn --reload 的监听 socket 由 reloader 父进程创建并传给 server 子进程:
# 只 kill 监听者会让父进程立刻重生一个新子进程, 旧实例根本没退出 —— 每次重启
# 就多叠一套常驻 1GB+ 的后端。这里把父、子一起结束。
# 只处理同一端口的实例: 换端口再起一个 (如临时开 8000 调试) 不该被顺手杀掉。
kill_repo_backends() {
  local pattern="$BACKEND_DIR/.venv/bin/python -m uvicorn app.main:app"
  local pids others=""
  pids=$(pgrep -f "$pattern" 2>/dev/null || true)
  if [ -z "$pids" ]; then
    return 0
  fi
  local to_kill=""
  local pid cmd
  for pid in $pids; do
    cmd=$(ps -o command= -p "$pid" 2>/dev/null || true)
    if [[ "$cmd" == *"--port $BACKEND_PORT"* ]]; then
      to_kill="$to_kill $pid"
    else
      others="$others $pid"
    fi
  done
  if [ -n "$others" ]; then
    warn "检测到其他端口上的本项目后端实例 (PID:$others), 未处理。"
    warn "如需一并结束: kill$others"
  fi
  if [ -z "$to_kill" ]; then
    return 0
  fi
  warn "结束本项目同端口后端实例 PID:$(echo "$to_kill" | xargs)"
  echo "$to_kill" | xargs kill 2>/dev/null || true
  sleep 1
  local left
  left=$(echo "$to_kill" | xargs -n1 sh -c 'kill -0 "$0" 2>/dev/null && echo "$0"' || true)
  if [ -n "$left" ]; then
    echo "$left" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
  return 0
}

# 先按仓库路径结束整组后端, 再查端口:
# 反过来的话 free_port 会先杀掉监听者 (reload 的子进程), 父进程随即重生一个,
# 端口检查看到的又是新进程。
kill_repo_backends
free_port backend  "$BACKEND_PORT"
free_port frontend "$FRONTEND_PORT"

# ===== 3. 依赖安装 =====
if [ ! -d "$BACKEND_DIR/.venv" ] || [ "${#BACKEND_EXTRA_ARGS[@]}" -gt 0 ]; then
  if [ "${#BACKEND_EXTRA_ARGS[@]}" -gt 0 ]; then
    info "同步后端 Python 依赖，extras: $BACKEND_EXTRAS"
  else
    info "后端首次启动 — 安装 Python 依赖(约 1-2 分钟)..."
  fi
  # macOS 自带 bash 3.2 在 set -u 下展开空数组会报 unbound variable,
  # ${arr[@]+"${arr[@]}"} 守卫:数组为空时展开为零个参数,非空时逐个带引号展开。
  ( cd "$BACKEND_DIR" && uv sync --frozen ${BACKEND_EXTRA_ARGS[@]+"${BACKEND_EXTRA_ARGS[@]}"} )
  ok "后端依赖装好了"
fi

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  info "前端首次启动 — 安装 Node 依赖..."
  ( cd "$FRONTEND_DIR" && pnpm install )
  ok "前端依赖装好了"
fi

# ===== 4. 启动 + 日志前缀 =====
PIDS=()

# 结束整棵子树: 只 kill 子 shell 自己会把 uvicorn/vite 留在原地变成孤儿
# (实测存在存活数天的残留 vite)。递归先杀后代再杀父进程。
kill_tree() {
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    kill_tree "$child"
  done
  kill "$pid" 2>/dev/null || true
}

cleanup() {
  echo
  info "关闭服务..."
  for pid in "${PIDS[@]:-}"; do
    if [ -n "$pid" ]; then
      kill_tree "$pid"
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
echo -e "${BLUE}│${NC}  backend   ${YELLOW}http://$DISPLAY_HOST:$BACKEND_PORT${NC}          ${BLUE}│${NC}"
echo -e "${BLUE}│${NC}  frontend  ${YELLOW}http://$DISPLAY_HOST:$FRONTEND_PORT${NC}          ${BLUE}│${NC}"
echo -e "${BLUE}│${NC}                                              ${BLUE}│${NC}"
echo -e "${BLUE}│${NC}  Ctrl-C 同时关闭两端                          ${BLUE}│${NC}"
echo -e "${BLUE}╰──────────────────────────────────────────────╯${NC}"
echo

(
  cd "$BACKEND_DIR"
  # --no-sync: 跳过依赖解析, 直接用已安装的 .venv。
  # 比 --frozen 更彻底: 不校验 lockfile, 避免镜像源 403/网络抖动导致后端起不来。
  # python -m uvicorn: 强制用 venv 的解释器和 uvicorn 模块, 防止 PATH 里
  # 其他 Python(如 /usr/local/bin/uvicorn) 抢先, 导致用错误版本启动后端。
  uv run --no-sync python -m uvicorn app.main:app ${UVICORN_ENV_ARGS[@]+"${UVICORN_ENV_ARGS[@]}"} --reload \
    --host "$BACKEND_HOST" --port "$BACKEND_PORT" 2>&1 \
    | prefix_awk "$(printf "${BLUE}[backend ]${NC} ")"
) &
PIDS+=("$!")

(
  cd "$FRONTEND_DIR"
  BACKEND_HOST="$BACKEND_HOST" BACKEND_PORT="$BACKEND_PORT" \
    pnpm dev --host "$BACKEND_HOST" --port "$FRONTEND_PORT" 2>&1 \
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
