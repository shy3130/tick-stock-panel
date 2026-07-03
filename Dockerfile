# 两阶段构建:前端 dist 拷进后端镜像,单容器运行
# 可选:构建网络无法直连官方源时,传入 --build-arg USE_CN_MIRROR=1 启用国内镜像
ARG USE_CN_MIRROR=1
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

# === Stage 2: Python 运行时 ===
FROM python:3.11-slim AS runtime
ARG USE_CN_MIRROR=1
ARG PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PYPI_FALLBACK=https://mirrors.aliyun.com/pypi/simple
ARG BACKEND_EXTRAS=
WORKDIR /app

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
# 两个关键路径, 确保 static / data 都指向容器内正确位置。
COPY backend/app ./app
ENV STATIC_DIR=/app/static \
    DATA_DIR=/app/data

# Frontend 静态产物
COPY --from=frontend-builder /build/dist ./static

ENV PYTHONPATH=/app
EXPOSE 3018
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3018"]
