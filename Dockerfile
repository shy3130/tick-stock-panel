# 两阶段构建:前端 dist 拷进后端镜像,单容器运行
# 可选:构建网络无法直连官方源时,传入 --build-arg USE_CN_MIRROR=1 启用国内镜像
# 可选:stock-sdk 插件默认不打包(它抓取第三方财经网站接口,存在版权与反爬风险)。
#       如确需启用,传入 --build-arg INCLUDE_STOCKSDK=1 显式开启,使用风险自负。
ARG USE_CN_MIRROR=1
ARG INCLUDE_STOCKSDK=0
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
# 备用 PyPI 源:主源同步延迟/故障时自动兜底(阿里云与清华互为补充)
ARG PYPI_FALLBACK=https://mirrors.aliyun.com/pypi/simple
ARG BACKEND_EXTRAS=

# === Stage 1: 前端构建 ===
FROM node:20-alpine AS frontend-builder
ARG USE_CN_MIRROR=1
ARG NPM_REGISTRY=https://registry.npmmirror.com
WORKDIR /build
# 关键:corepack 不读 npm 的 registry 配置,且跨 RUN 不保留环境变量,
# 因此国内网络下最稳的做法是直接用 npm 安装 pnpm(npm 会读取 .npmrc 镜像源),
# 彻底绕开 corepack 再次联网下载 pnpm 的问题。
RUN if [ "$USE_CN_MIRROR" = "1" ]; then npm config set registry "$NPM_REGISTRY"; fi && \
    npm install -g pnpm@9
# 让 pnpm 走镜像源安装依赖
RUN if [ "$USE_CN_MIRROR" = "1" ]; then pnpm config set registry "$NPM_REGISTRY"; fi
COPY frontend/package.json frontend/pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile || pnpm install
COPY frontend/ ./
RUN pnpm build

# === Stage 1b: stock-sdk 插件依赖(可选,默认跳过) ===
# ⚠️ 合规提示: stock-sdk 通过 node bridge.mjs 抓取第三方财经网站(如东方财富)的行情接口,
#    未经对方授权,可能违反其服务条款并涉及交易所行情版权。默认不打包(INCLUDE_STOCKSDK=0)。
#    如确需启用,构建时传 --build-arg INCLUDE_STOCKSDK=1,即视为使用者知悉并自行承担合规责任。
# INCLUDE_STOCKSDK=0 时,本 stage 仅产出空 node_modules 目录,保证后续 COPY 不报错。
FROM node:20-bookworm-slim AS stocksdk-builder
ARG USE_CN_MIRROR=1
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG INCLUDE_STOCKSDK=0
WORKDIR /build
RUN if [ "$USE_CN_MIRROR" = "1" ]; then npm config set registry "$NPM_REGISTRY"; fi
COPY backend/app/plugins/stocksdk/package.json backend/app/plugins/stocksdk/package-lock.json ./
# INCLUDE_STOCKSDK=1 时安装依赖;=0 时仅建空目录,使最终镜像不含 stock-sdk 依赖
RUN if [ "$INCLUDE_STOCKSDK" = "1" ]; then \
      (npm ci || npm install); \
    else \
      mkdir -p /build/node_modules; \
    fi

# === Stage 2: Python 运行时 ===
FROM python:3.11-slim AS runtime
ARG USE_CN_MIRROR=1
ARG PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PYPI_FALLBACK=https://mirrors.aliyun.com/pypi/simple
ARG BACKEND_EXTRAS=
ARG INCLUDE_STOCKSDK=0
WORKDIR /app

# Node.js 运行时: 仅在启用 stock-sdk 插件时安装(供 node bridge.mjs 使用)。
# bookworm 自带 nodejs 18.19, 满足插件 engines>=18; --no-install-recommends 精简,
# 自带 libnode/libc-ares 等全部动态依赖, 无需手动补库。
# 国内构建走 apt mirror 已在 debian 镜像sources.list 配好, 无需额外换源。
RUN if [ "$INCLUDE_STOCKSDK" = "1" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends nodejs \
      && rm -rf /var/lib/apt/lists/* \
      && node --version; \
    fi

# 安装 uv(快) —— 国内镜像下三重兜底:主源 → 备用源 → 官方源,
# 任一成功即可,避免单一镜像同步延迟/故障导致构建失败。
# uv 发版极频繁,国内镜像同步存在时间窗口,不锁版本且无 fallback 时
# 容易遇到 "from versions: none"(索引解析不到最新版)。
RUN if [ "$USE_CN_MIRROR" = "1" ]; then \
      pip install --no-cache-dir uv -i "$PYPI_INDEX" || \
      pip install --no-cache-dir uv -i "$PYPI_FALLBACK" || \
      pip install --no-cache-dir uv; \
    else \
      pip install --no-cache-dir uv; \
    fi

# Backend deps
COPY README.md /README.md
COPY backend/pyproject.toml backend/uv.lock* ./
# uv 原生支持同时挂多个 index(主源 + 备用源),会自动在两源中查找,
# 比逐个重试更稳健 —— 任一源缺包时另一源补位。
RUN if [ "$USE_CN_MIRROR" = "1" ]; then \
      export UV_DEFAULT_INDEX="$PYPI_INDEX" UV_EXTRA_INDEX_URL="$PYPI_FALLBACK"; \
    fi; \
    set -- --no-dev; \
    for extra in $BACKEND_EXTRAS; do \
      set -- "$@" --extra "$extra"; \
    done; \
    uv sync --frozen "$@" || uv sync "$@"

# Backend code
# 注意:Docker 里 WORKDIR=/app, 而 config.py 的 _PROJECT_ROOT 是按开发布局
# (<root>/backend/app/) 推导的, 容器内会错算到 /。这里用环境变量显式指定
# 三个关键路径, 确保 static / tiers / data 都指向容器内正确位置。
COPY backend/app ./app
# stock-sdk 插件依赖: 从 stocksdk-builder 拷入。
# INCLUDE_STOCKSDK=0(默认) 时, stocksdk-builder 产出空目录,此处拷入空目录,
# 即最终镜像不含 stock-sdk 依赖,插件默认不可用。
# COPY --from 不受 .dockerignore 的 **/node_modules 规则影响。
COPY --from=stocksdk-builder /build/node_modules ./app/plugins/stocksdk/node_modules
COPY tiers.yaml /app/tiers.yaml
ENV STATIC_DIR=/app/static \
    TIERS_YAML=/app/tiers.yaml \
    DATA_DIR=/app/data

# Frontend 静态产物
COPY --from=frontend-builder /build/dist ./static

ENV PYTHONPATH=/app
# 兜底时区: 交易时段判断已在代码里显式用北京时间 (app/market_time.py),
# 此处让日志时间戳等其余 naive 时间也对齐北京时间。
ENV TZ=Asia/Shanghai
EXPOSE 3018
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3018"]
