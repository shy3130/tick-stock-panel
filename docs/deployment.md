# 部署指南

本项目的几种运行方式，按推荐程度排序。配置项详解见 [configuration.md](./configuration.md)。

> 📌 前置依赖(仅方式 D 需要):Python ≥ 3.11 · Node ≥ 20 · [`uv`](https://docs.astral.sh/uv/) · `pnpm`（`npm i -g pnpm`）

---

## 方式 A:GHCR 现成镜像(免本地构建,多数用户推荐)

GitHub Actions 每次推送都会自动构建多架构镜像(linux/amd64 · arm64)并发布到 GHCR,直接拉取运行,本地无需 Python / Node,也不用现场 build:

```bash
docker run -d --name tsp -p 3018:3018 -e DATA_DIR=/app/data -v "${PWD}/data:/app/data" ghcr.io/shy3130/tick-stock-panel:latest
# 打开 http://localhost:3018
```

- 需要配置时:从 `.env.example` 复制出 `.env`,命令里加 `--env-file .env`,保留 `-e DATA_DIR=/app/data` 与数据挂载,避免旧镜像把 `DATA_DIR=./data` 解析到未挂载的 `/data`。
- 自定义绝对数据路径会原样保留,需要把宿主机目录挂载到相同目标。Compose 用户需要同时调整 `environment` 和 `volumes`,仅修改 `.env` 不会覆盖 Compose 中明确指定的路径。
- 自定义数据源等服务跑在宿主机、容器内需通过 `host.docker.internal` 访问时,Linux 的 `docker run` 要在命令里加 `--add-host=host.docker.internal:host-gateway`(Docker Desktop 的 macOS/Windows 默认已可解析;方式 B 的 compose 已在 `extra_hosts` 配好),否则报 `Name or service not known`。
- 镜像默认不含 `legacy-cpu` / `backtest` extras —— 老 CPU(无 AVX2)或需要 vectorbt 回测时,请用方式 B 通过 `BACKEND_EXTRAS` 自构建。
- 跑自己改过的代码:fork 后到仓库 Actions 页启用 workflow(fork 默认禁用),构建出的 `ghcr.io/<你的用户名>/tick-stock-panel` 用法相同。
- 想要 compose 全套挂载(`.env` / `tiers.yaml` / 数据卷):参考根目录 `docker-compose.yml`,把 `build:` 段换成 `image: ghcr.io/shy3130/tick-stock-panel:latest`。

更新到新版本:

首次升级、曾使用 `--env-file` 或不确定挂载是否正确时,先完成下方的[数据目录核对与备份](#docker-升级前核对数据目录)。

```bash
docker pull ghcr.io/shy3130/tick-stock-panel:latest
docker rm -f tsp
# 重新执行原有的完整 docker run,保留 --env-file、自定义 -e 和原有数据挂载
```

---


## 方式 B:Docker Compose(本地构建,全套挂载)

```bash
cp .env.example .env
docker compose up --build
# 打开 http://localhost:3018
```

Docker 采用两阶段构建,前端 dist 拷进后端镜像,**单容器**运行,数据完全在自己手里。

更新到新版本:

```bash
git pull
docker compose up --build -d
```

---

## 方式 C:本机 AI 代部署(小白推荐)

装一个本机 AI 编程助手(Trae / Codex / OpenCode / ZCode / WorkBuddy 等,任选其一),把 [README · 快速开始](../README.md#-快速开始) 里方式 C 的提示词原样发给它,AI 会自动完成克隆、装依赖、启动服务。适合完全不想碰命令行的用户;AI 最终执行的仍是方式 A / B / D 之一。

---


## 方式 D:Dev 模式(二次开发推荐)

由于刚开源近期更新频繁,推荐开发模式运行,可随时 `git pull` 同步最新代码。

```bash
git clone https://github.com/shy3130/tick-stock-panel.git
cd tick-stock-panel
cp .env.example .env       # 按需填 TICKFLOW_API_KEY(留空 = None 模式)
./dev.sh                   # Windows: .\dev.ps1
```

`dev.sh` 自动检查 / 下载依赖、释放端口、同时起前后端,Ctrl-C 一并关闭。默认:

- 后端 → <http://localhost:3018> · 前端 → <http://localhost:3011>
- 自定义端口:`BACKEND_PORT=8000 FRONTEND_PORT=5173 ./dev.sh`

### 手动分别启动(不想用 dev.sh)

```bash
# 后端
cd backend && uv sync --extra backtest   # 含回测依赖
# 老 CPU: uv sync --extra legacy-cpu
# 老 CPU + 回测: uv sync --extra legacy-cpu --extra backtest
uv run uvicorn app.main:app --reload --port 3018

# 前端
cd frontend && pnpm install && pnpm dev   # http://localhost:3011
```

---

## 老 CPU 兼容(avx2/fma 缺失)

如果运行时报 `avx2`/`fma` 缺失,或进程 `exit 132`,说明 CPU 不支持 AVX2 指令集(常见于老 VPS)。解决:

- **Dev 源码启动**:在根目录 `.env` 设置后运行 `./dev.sh` 或 Windows 的 `.\dev.ps1`;即使已有 `.venv`,启动器也会同步兼容内核
- **Docker**:在根目录 `.env` 设置后执行 `docker compose up --build`

```ini
BACKEND_EXTRAS=legacy-cpu          # 兼容老 CPU
BACKEND_EXTRAS=legacy-cpu backtest # 兼容老 CPU + 回测依赖
```

手动启动源码时，也可以在 `backend/` 目录直接执行 `uv sync --extra legacy-cpu`。不要设置 `POLARS_SKIP_CPU_CHECK`，它只会隐藏警告，实际执行不支持的指令时仍可能崩溃。

### 回测依赖说明

vectorbt → numba 体积较大,作为可选 extras(`uv sync --extra backtest`)。macOS / Intel 无预构建 wheel 时需 `brew install cmake` 现场编译。

---

## 更新代码(已部署用户必读)

拉取新版本只需一条命令(Dev / Compose 本地构建用户):

```bash
git pull
```

> 用方式 A 镜像直跑(无本地仓库)的用户:`docker pull ghcr.io/shy3130/tick-stock-panel:latest` 后删除旧容器重跑;compose 换 `image:` 的用户执行 `docker compose pull && docker compose up -d`。

> ⚠️ **方式 A 重建容器时必须保留 `-e DATA_DIR=/app/data` 与原有的宿主机数据目录挂载。** 更换启动目录时,相对的 `${PWD}/data` 也会指向另一份数据;建议使用固定宿主机绝对路径。自定义容器数据目录时,环境变量和挂载目标须一致。

### Docker 升级前核对数据目录

数据源、AI 与扩展数据的 Key 保存于实际 `DATA_DIR` 下的 `user_data/secrets.json`,其他配置与行情也使用这个数据根目录。检查时只输出路径,不要把凭据文件内容贴到日志或反馈中。

```bash
# 容器名若不是 tsp,请替换为自己的容器名。
docker exec tsp /app/.venv/bin/python -c "from app.config import settings; print(settings.data_dir)"
docker inspect tsp --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
```

实际数据目录应位于挂载目标内,且升级前后使用同一份宿主机目录。新版镜像会将相对路径按应用目录解析,仍保留已经挂载的旧路径和用户配置的绝对路径。发现未挂载的旧目录中有数据时会停止切换,要求先备份;程序不会自动搬迁或覆盖文件。未能确认持久化挂载时会输出启动提醒,不影响源码版与桌面版。

如果旧容器的实际目录是 `/data`,但挂载目标只有 `/app/data`,先保留旧容器并备份。以下备份目标应是一个尚未使用的新目录:

```bash
docker stop tsp
docker cp tsp:/data ./tsp-data-backup
```

核对备份中的 `user_data`、行情与扩展数据后,手动合并到实际挂载的宿主机数据目录;已有文件存在冲突时先核对,不要整目录覆盖。确认数据和 Key 均已迁出后,再按正确的路径重建容器。旧容器已删除且没有持久卷或备份的文件,此次修复无法自动找回。

**整个 `data/` 目录都不纳入 git** —— 行情 K线、财务、自选、回测、监控记录,乃至概念/行业扩展数据,全部是程序运行时生成/拉取的用户数据,`git pull` 物理上无法影响它们。新用户首次启动时,概念/行业两份扩展数据会自动从远程接口拉取,无需任何手动操作。

> ⚠️ **切勿使用以下命令"解决冲突"或"清理",它们会一次性删光 `data/` 下所有未被 git 跟踪的数据:**
> - `git clean -fdx`(最危险,会删掉所有 `.gitignore` 忽略的文件)
> - `git reset --hard`
> - 直接删除整个项目文件夹重新 `git clone`
>
> 若 `git pull` 报冲突,通常是本地误改了被跟踪的文件,请先 `git stash` 暂存再 pull,或单独联系作者,不要直接执行上面的命令。

---

## 桌面版覆盖升级与旧配置恢复

Windows 覆盖安装会沿用安装器登记的原安装位置；只有新装时才默认选择 `D:\TSP`，没有 D 盘则使用当前用户的应用目录。显式指定 `/DIR` 或在安装向导中另选位置仍然有效。安装包排除运行时 `data/`，不会用打包环境中的数据覆盖用户配置。

桌面版在启动后端前检查旧目录，包括 Windows 历史的安装目录兄弟 `TickFlowStockPanel_Data`、旧兼容位置，以及 macOS 旧 `.app` 包内的 `data`。当前 `data/user_data/user_data` 中被旧版多套了一层或多层的配置也会恢复到正确位置。

恢复遵循以下优先级：

- 已有密钥文件整份保留，避免把旧服务地址、账号和新 Key 拼成一套配置；密钥文件缺失时才从旧目录恢复。
- 偏好只补缺失字段，现有值（包括 `false`、`0`、空字符串和空列表）保持不变；改写前保留新旧文件备份。
- 自选、策略、扩展配置等普通文件同名时不覆盖；仅补齐缺失文件。旧配置原件保留，缺失的历史行情分区可按文件迁入，已存在的分区不变。
- `data/upgrade_backups/data-dir-v1/` 保存恢复记录和配置备份；完成过的恢复不会在以后重启时重新加入用户主动删除的 Key。这些备份可能含 Key，不应公开分享。

如果此前已经安装到了另一位置，新安装器只能继承当前登记的位置；可在向导中选择仍保留旧 `data` 的原安装目录。程序不会扫描全部磁盘或擅自选择多套数据中的一套。无法写入或检测到配置在恢复期间被其他程序修改时，会保留旧文件并停止本次恢复。

## 访问密码设置(公网部署必读)

面板部署在公网服务器时,首次设置访问密码有限制 —— **必须从本机或内网访问**,以防公网上陌生人抢先设置密码锁死你的面板。

如果你在公网浏览器直接打开页面,会看到提示:

> 首次设置密码仅允许本机或内网访问,请通过 SSH/本地浏览器操作

有两种方式解决,任选其一。

### 方式一:环境变量预置密码(最简单,推荐)

在 `.env` 文件(或 Docker / 系统环境变量)里设置 `AUTH_PASSWORD`:

```bash
AUTH_PASSWORD='你的密码'
```

然后重启服务。启动时会自动:

1. 读取 `AUTH_PASSWORD`
2. 用 PBKDF2 哈希后写入 `auth.json`(`chmod 600`,只存哈希不存明文)
3. **之后这个环境变量就不再被读取** —— 是一次性的初始化

设完后即可用公网地址 + 这个密码正常登录。后续改密码请用页面 UI(`设置 → 修改密码`),不受环境变量影响。

**注意事项:**

- **密码至少 6 位**,否则会被跳过并记一条 warning 日志
- **仅在未设过密码时生效**。已设过密码后,改这里不会覆盖(避免重启时重置你在 UI 改的密码)
- 密码建议使用单引号包裹，避免 Docker Compose 插值 `$VAR`；启动时也会从只读挂载的原始 `.env` 初始化，兼容已有的未加引号配置
- `.env` 文件权限保持 `600`,**不要提交到 Git**
- 明文密码只存在于 `.env` / 环境变量中,落盘的是哈希,安全性等同 `auth.json`

**重置密码(忘密码时):** 删除或清空 `data/user_data/auth.json`,重启服务,会回到"未设密码"状态,此时 `AUTH_PASSWORD` 会重新生效。

```bash
rm data/user_data/auth.json   # 停服后执行,清空后重启
```

### 方式二:SSH 端口转发

不用改配置,在你**自己电脑**的终端执行(不是服务器上):

```bash
ssh -L 3018:127.0.0.1:3018 用户名@服务器IP
```

例如服务器是 `123.45.67.89`、用户名 `root`、面板端口 `3018`:

```bash
ssh -L 3018:127.0.0.1:3018 root@123.45.67.89
```

保持这个 SSH 连接**不要关**,然后在**自己电脑的浏览器**打开 `http://127.0.0.1:3018`。此时后端看到的客户端 IP 是 `127.0.0.1`(本机),能通过校验,正常显示设置密码界面。

**设完密码后**,SSH 连接可以断开 —— 密码已存进服务器,之后直接用公网地址 + 刚设的密码访问即可。

> 如果用 `PORT` 改过端口(比如 `PORT=8080`),两处都要替换:`ssh -L 8080:127.0.0.1:8080 root@IP`。

### 两种方式怎么选

| | 环境变量 | SSH 转发 |
|---|---|---|
| 操作 | 改一行配置 + 重启 | 一条 ssh 命令 |
| 需要改配置 | 是 | 否 |
| 适合 | Docker / 自动化部署 / 不熟 SSH | 临时设密码 / 能 SSH 到服务器 |
| 后续改密码 | UI(`设置 → 修改密码`) | 同左 |

推荐**方式一(环境变量)**,一次配置即可,Docker 部署尤其方便。

### 原理说明

- **为什么限制本机/内网?** 面板部署到公网后,任何人都能访问 URL。如果不限制,攻击者可以在你之前打开页面、设置一个密码,把你的面板锁死。
- **本机/内网如何判断?** 后端检查客户端 IP 是否属于 `127.0.0.1 / ::1 / 10.x / 192.168.x / 172.16-31.x`。
- **SSH 转发为什么有效?** `-L` 把本机端口通过 SSH 隧道转发到服务器的 `127.0.0.1`,等同于在服务器本地访问,客户端 IP 变成 `127.0.0.1`,通过校验。
- **反向代理注意:** 若面板在 Nginx 等反代之后,需正确配置 `X-Forwarded-For` 头,后端据此取真实客户端 IP。

## 多窗口与多人共享访问

同一部署可以由多个浏览器窗口、设备同时访问。API 主进程保持 **1 个**
(`uvicorn --workers 1`，现有启动方式默认如此)，计算子进程继续沿用回测/优化的
既有机制。当前队列、缓存与限额在进程内共享，不能通过增加 API workers 或让多个
容器同时写同一数据目录来扩容。登录和用户数据仍采用现有单密码、共享数据模式。

### 请求和计算的区别

- 行情统一采集并广播；每个 SSE 连接异步等待，不独占一个等待线程。
- 策略页自动加载和查看单策略时发送可选的 `ensure: true`：相同输入正在计算时复用
  执行，有效结果短期复用。旧客户端不传该字段仍保留手动重跑语义。
- 自动结果最长复用 6 秒，并检查数据版本、策略定义、有效参数、资产、周期、日期
  等输入。策略代码热重载、参数修改和日K修补后不复用旧输入的结果。
- 重叠策略批次可复用已计算的单策略结果；不同业务输入独立计算。
- 分钟批量查询将同一数据源、资产、价格基准、时间范围和标的的补拉合并；重叠标的
  不重复并发拉取或落盘。本地分区变化使旧补拉缓存失效，结果最多缓存 2 秒。
  `since` 仍只控制返回增量；原始价落盘、读取时复权以及残缺数据回退语义保持不变。
- 交易日探针的冷请求共享一次探测。上游缓慢时，等待者短暂等待后按现有“未知”
  语义降级，避免占满 HTTP 线程；交易日、休市、未知的原有 TTL 不变。
- 轮询任务状态只读取状态，不创建新任务。新客户端按后端任务 ID 跟踪完成、失败，
  不把其他批次的旧缓存当成本次完成。

### 过载时的行为

| 项目 | 默认边界 |
| --- | --- |
| 策略批量任务 | 最多 32 个活跃/排队批次，串行执行 |
| 策略输入准备和计算 | 共用重任务独占额度，兼容仓库冷启动预热 |
| 重任务等待队列 | 最多 32 个等待者，默认最多等待 120 秒 |
| 回测/优化 SSE 任务 | 最多 32 个活跃任务；完成结果最多保留 16 条、5 分钟 |
| 已登录会话或 Token | 每分钟最多新建 30 个策略/回测计算任务；读取、订阅和复用不重复扣额度 |
| 分钟补拉结果 | 最多 8192 个标的/范围缓存项，总估算数据大小不超过 32 MiB |

队列满或等待超时时返回 `503` 和 `Retry-After`，前端退避重试。已缓存数据的读取和
健康检查不占计算额度。自动计算失败有短暂冷却，任务失败、取消或启动失败会释放占用。
未设密码的本机部署保持原有会话行为，同时仍受全局计算容量约束。

SSE 合并重复版本通知；隐藏页面暂停相关行情/策略数据回拉，恢复可见后检查数据。
策略列表实时价格仍保留轮询，避免未加入监控的策略漏更新；轮询与 SSE 重连加入错峰。
Parquet、策略文件和既有配置无需迁移，新响应元数据均为可选字段。

### 观测与验证

登录后可读取 `GET /api/data/workload`，查看请求量、实际执行量、复用/缓存命中、
失败次数、队列、缓存和 SSE 订阅数。统计为进程级、有界计数，不记录请求参数、会话
凭据或用户数据，重启后清零；此接口沿用面板认证，不额外开放 Token scope。

相关回归与可重复的模拟 HTTP 压测：

```bash
cd backend
uv run --frozen pytest tests/test_workload.py tests/test_shared_work.py \
  tests/test_strategy_run_queue.py tests/test_screener_run_all_progressive.py \
  tests/test_minute_shared_pull.py tests/test_kline_minute_batch_pull_fallback.py \
  tests/test_heavy_job_limiter.py tests/test_backtest_job_admission.py \
  tests/test_strategy_realtime_refresh.py tests/test_trading_day.py -q
uv run --frozen pytest tests/test_multi_client_http.py -q -s
```

HTTP 压测使用隔离存储和计数用的模拟策略引擎，覆盖 1/10/30/100 个客户端、
完成后再次访问、单策略查看、轮询、手动重跑、不同任务过载和健康检查。
它验证调度与去重，不能据此推断真实历史数据下的可承载人数。上线容量仍需在目标
服务器上按实际数据量、页面组合、任务规模和持续时间测量 CPU、内存与响应延迟。
