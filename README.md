<div align="center">

# 📈 A股智能量化工作台

**自托管、零运维的 A 股「选股 + 监控 + 回测」量化工作台**

**面向个人散户与量化爱好者而生**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-≥3.11-blue.svg)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-18-61dafb.svg)](https://react.dev/)
[![Data: TickFlow](https://img.shields.io/badge/Data-TickFlow-00b386.svg)](https://tickflow.org/auth/register?ref=V3KDKGXPEA)
[![Deploy: Docker](https://img.shields.io/badge/Deploy-Docker-2496ed.svg)](./Dockerfile)
[![GitHub stars](https://img.shields.io/github/stars/shy3130/tickflow-stock-panel?style=social)](https://github.com/shy3130/tickflow-stock-panel/stargazers)

</div>

<div align="center">

**[快速开始](#-快速开始)** · **[核心功能](#-核心功能)** · **[配置](#️-配置)** · **[路线图](#-路线图)**

</div>

- 🆓 **开箱即用** — 留空 Key 即进 None 模式,历史日 K 免费体验,**无需付费**
- 🏠 **自托管零运维** — Docker 单容器部署,数据完全掌握在自己手里
- 🔍 **三位一体** — 选股(20 内置策略)+ 实时监控 + 向量化回测,Polars 毫秒级扫描全 A 股
- 🤖 **AI 加持** — 一句话生成策略代码,任意 OpenAI 兼容接口均可接入(留空即关闭)
- 🔌 **自由扩展** — 自有量化项目数据,与内置数据同台分析
- 🇨🇳 **A 股专用** — 盘后自动AI复盘并推送至飞书等;连板梯队、涨停动量、内置ths 概念 / 行业



 基于 [TickFlow](https://tickflow.org/auth/register?ref=V3KDKGXPEA) 数据源。**明确不做**:不对标同花顺 / 通达信,不内置「AI 荐股 / 涨停预测」。

> ⚠️ 考虑到tickflow数据源没有人气/资金流向等个性化数据,我将开放自有的第三方数据以供大佬们研究使用,包括但不限于当前内置的ths概念/ths行业(后续更新在这里)

 
> 有更多稳定免费数据源推荐,或者提交建议/意见的大佬可以邮件到 415333856@qq.com,q群 109338242


觉得有用可以点个 Star，蟹蟹 🌹

---

## 🎯 项目定位

**面向个人散户与量化爱好者的 A 股分析工作台**,聚焦「**选股 + 监控 + 回测**」三大场景,LLM能力驱动进行市场分析，掌控市场节奏；让普通投资者也能拥有一套可自定义策略的量化工具。

---

## 📸 界面预览

<table>
  <tr>
    <td width="50%" align="center"><b>看板 Dashboard</b></td>
    <td width="50%" align="center"><b>策略 Screener</b></td>
  </tr>
  <tr>
    <td width="50%"><img src="./screenshots/dashboard.png" alt="看板页面"></td>
    <td width="50%"><img src="./screenshots/screener.png" alt="策略页"></td>
  </tr>
  <tr>
    <td width="50%" align="center"><b>回测 Backtest</b></td>
    <td width="50%" align="center"><b>监控中心 Monitor</b></td>
  </tr>
  <tr>
    <td width="50%"><img src="./screenshots/backtest.png" alt="回测页"></td>
    <td width="50%"><img src="./screenshots/monitor.png" alt="监控中心"></td>
  </tr>
  <tr>
    <td width="50%" align="center"><b>连板梯队 Limit Ladder</b></td>
    <td width="50%" align="center"><b>概念分析 Concept</b></td>
  </tr>
  <tr>
    <td width="50%"><img src="./screenshots/limit-ladder.png" alt="连板梯队页"></td>
    <td width="50%"><img src="./screenshots/concept-analysis.png" alt="概念分析"></td>
  </tr>
</table>

<div align="center">

### 📸 [查看更多界面截图 »](./screenshots/README.md)

</div>

---

## 🚀 快速开始

### 前置依赖

| 工具                               | 版本   | 安装                                               |
| :--------------------------------- | :----- | :------------------------------------------------- |
| Python                             | ≥ 3.11 | [python.org](https://www.python.org/)              |
| Node                               | ≥ 20   | [nodejs.org](https://nodejs.org/)                  |
| [`uv`](https://docs.astral.sh/uv/) | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `pnpm`                             | 9      | `npm i -g pnpm`                                    |

### 方式 A:Dev 模式(二次开发推荐)

```bash
cp .env.example .env       # 按需填 TICKFLOW_API_KEY(留空 = None 模式)
./dev.sh                   # Windows: .\dev.ps1
```

自动检查 / 下载依赖、释放端口、同时起前后端,Ctrl-C 一并关闭。默认:

- 后端 → <http://localhost:3018> · 前端 → <http://localhost:3011>
- 自定义端口:`BACKEND_PORT=8000 FRONTEND_PORT=5173 ./dev.sh`

### 方式 B:Docker(部署最省心)

```bash
cp .env.example .env
docker compose up --build
# 打开 http://localhost:3018
```

<details>
<summary><b>环境适配与高级选项(老 CPU · 手动启动 · 回测依赖)</b></summary>

**老 CPU 兼容(avx2/fma 缺失报错或 exit 132)**:桌面客户端安装包已内置兼容内核(新老 CPU 通吃)。Docker / 源码用户在 `.env` 打开 `BACKEND_EXTRAS=legacy-cpu` 后重建,会给 Polars 切到 `rtcompat` 运行时;需回测则 `BACKEND_EXTRAS=legacy-cpu backtest`。

**手动分别启动:**

```bash
# 后端
cd backend && uv sync --extra backtest   # 含回测依赖
uv run uvicorn app.main:app --reload --port 3018

# 前端
cd frontend && pnpm install && pnpm dev   # http://localhost:3011
```

**回测依赖**:vectorbt → numba 体积较大,作为可选 extras(`uv sync --extra backtest`)。macOS / Intel 无预构建 wheel 时需 `brew install cmake` 现场编译。

</details>

### 🔄 更新代码(已部署用户必读)

拉取新版本只需一条命令:

```bash
git pull
```

**整个 `data/` 目录都不纳入 git**——行情 K线、财务、自选、回测、监控记录,乃至概念/行业扩展数据,全部是程序运行时生成/拉取的用户数据,`git pull` 物理上无法影响它们。新用户首次启动时,概念/行业两份扩展数据会自动从远程接口拉取,无需任何手动操作。

> ⚠️ **切勿使用以下命令"解决冲突"或"清理",它们会一次性删光 `data/` 下所有未被 git 跟踪的数据:**
> - `git clean -fdx`(最危险,会删掉所有 `.gitignore` 忽略的文件)
> - `git reset --hard`
> - 直接删除整个项目文件夹重新 `git clone`
>
> 若 `git pull` 报冲突,通常是本地误改了被跟踪的文件,请先 `git stash` 暂存再 pull,或单独联系作者,不要直接执行上面的命令。

### 🧭 跑起来后的第一次使用

1. **设置 → 凭据与能力** → 点 **重新检测**,确认档位标签
2. **设置** → **立即跑盘后管道**:拉日 K + 计算 enriched 表(None / Free 走 free-api,当日数据盘后 1-2 小时可用)
3. **自选**页加标的 → **选股**页点策略卡片扫描 / 配自定义信号
4. **回测**页选策略 + 区间 → 看净值 / 夏普 / 交易明细(SSE 实时进度)
5. **监控中心**配规则(策略 / 个股信号 / 价格 / 异动),盘中实时弹窗 + 持久化记录

---

## ✨ 核心功能

### 🔍 选股引擎(Screener)

**20 个内置策略**,每个策略一个独立 Python 文件,基于 Polars 表达式向量化实现(`backend/app/strategy/builtin/`):

| 类型        | 代表策略                                                 |
| :---------- | :------------------------------------------------------- |
| 趋势 / 形态 | 趋势突破 · 均线多头 · MA 金叉 · MACD 金叉放量 · 布林突破 |
| 量价 / 涨停 | 量价齐升 · 高换手强势 · 连板股 · 断板反包 · 涨停动量     |
| 反转 / 波动 | 超跌反弹 · 超卖反转 · 新低反转 · 低波动龙头 · 回踩 MA20  |

**扩展策略的三种方式:**

| 方式              | 说明                                                                                                  |
| :---------------- | :---------------------------------------------------------------------------------------------------- |
| **🎛️ 自定义信号** | 不写代码,UI 上 `字段 + 操作符 + 阈值` 组合编译成 Polars 表达式热加载                                  |
| **🤖 AI 生成**    | 一句话描述思路,LLM 读 `strategy-guide.md` 生成完整策略文件(经 `ast` 校验)→ 落入 `data/strategies/ai/` |
| **📝 代码迁移**   | 参照开发指南把已有策略改写为 Polars 文件放入 `data/strategies/custom/`,引擎自动发现                   |

### 📊 指标流水线(Indicators)

原生 Polars 向量化,全 A 股一次扫表落盘 enriched Parquet:

- **均线 / 趋势**:MA(5-60)· EMA · MACD · 动量 · 布林带
- **震荡 / 波动**:RSI · KDJ · ATR · 年化波动率 · 振幅
- **量能 / 涨跌停**:量比 · 量均线 · 涨停信号 · 连板数
- **原子信号**:MA / MACD 金叉死叉 · N 日新高新低 · 布林突破
- **复权**:基于除权因子自动前复权,回测与指标口径一致

### 🧪 回测引擎(Backtest)

基于 vectorbt:**三种模式**(个股 / 策略组合 / 自由信号组合),真实约束(T+1 · 手续费 · 滑点 · 止损 · 最大持仓天数),组合管理(最大持仓 · 敞口 · 等权 / 自定义仓位)。SSE 流式进度支持切页重连,输出净值曲线 · 夏普 · 最大回撤 · 胜率 · 交易明细。

### 📡 监控中心(Monitor)

统一规则引擎,一个页面管理**四类监控**(策略 · 个股信号 · 价格涨跌 · 全市场异动):

- 多条件 AND/OR + 冷却期去重 + 严重级别(info/warn/critical)
- 多入口配置:监控中心新建 / 个股详情页「加监控」/ 策略卡片一键开启
- 命中后右下角弹窗(可配声效)+ 持久化到 `alerts.jsonl`,菜单未读徽标
- **触发记录详情**:每条记录展示命中的具体条件(如 `RSI>80`)与当前价位,一眼看清为何触发
- **飞书 Webhook 推送**:全局一处配置飞书群机器人地址,启用推送的规则命中即推送到飞书群(支持签名校验);可在设置页设「默认推送渠道」,新建规则自动预填

### 📈 个股分析(Beta)

以「行情 + 关键价位」为主体的单标的决策页:

- **专用日 K 图表**:主图 + 成交量 + 滑块,默认近 6 个月
- **9 类关键价位**(纯函数实时计算,毫秒级):压力支撑 · 成交密集区 · 枢轴点 · 前高前低 · Keltner 通道 · ATR 止损 · 缺口位 · 斐波那契 · 整数关口
- **AI 四维分析**:技术 / 基本面 / 财务 / 消息面流式生成,实战派交易员视角

### 🧰 数据与扩展

- **TickFlow 多源数据**:日 K / 分钟 K / 指数 / 财务 / 实时行情
- **🔌 第三方接入(重点)**:Tushare 等 HTTP 定时拉取 · CSV / Excel 上传 · JSON 写入,自动 schema 发现 + 符号归一,页面可视化配置,**可与自有量化项目数据并入 DuckDB 同台分析**
- **盘后定时管道**:APScheduler 15:30 CST 自动拉日 K + 重算 enriched + 跑监控
- **令牌桶限流**:适配各档位 rpm / batch,批量合并 + 增量拉取

---

## ⚙️ 配置

所有配置从根目录 `.env` 读取(复制 `.env.example` 开始),也可在面板 **设置** 页修改。

### 数据源:TickFlow

```ini
TICKFLOW_API_KEY=              # 留空 = None 模式(历史日K免费);填 Key = 按订阅档位解锁
```

留空即 None 模式,通过 free-api 使用历史日 K(当日数据盘后 1-2 小时可用);免费注册 Key 后进 Free 模式,开启自选股实时监控。**实时行情按档位**:

| 档位     | 实时能力                                 |
| :------- | :--------------------------------------- |
| Free     | 自选页前 5 个标的实时监控(最低 6 秒刷新) |
| Starter+ | 全市场实时行情                           |
| Pro      | 分钟 K + 盘口                            |
| Expert   | WebSocket + 财务数据                     |

> 完整能力矩阵见 [tickflow.org/pricing](https://tickflow.org/pricing/),高等档位含较低档全部权益。

### AI(可选)

用于自然语言生成策略。**所有配置留空即跳过**,不影响核心功能。支持任意 OpenAI 兼容接口:

```ini
AI_PROVIDER=openai_compat              # openai_compat | ollama
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=                            # 留空 = 关闭 AI
AI_MODEL=deepseek-chat
AI_DAILY_TOKEN_BUDGET=500000           # 每日 token 预算上限
```

### 服务与数据

```ini
HOST=0.0.0.0          # 监听地址
PORT=3018             # 服务端口
LOG_LEVEL=INFO        # DEBUG | INFO | WARNING | ERROR
DATA_DIR=./data       # Parquet / DuckDB 数据存储目录
```

### 访问密码

面板首次设置访问密码时,出于安全考虑**仅允许本机或内网访问**(防公网陌生人抢先设置锁死面板)。公网服务器部署有两种方式设首个密码:

1. **环境变量预置(推荐)** — 在 `.env` 填入 `AUTH_PASSWORD`,首次启动自动初始化(哈希后写入 `auth.json`,之后不再读取):
   ```ini
   AUTH_PASSWORD=你的密码    # 至少 6 位;仅首次生效,已设过则不覆盖
   ```
2. **SSH 端口转发** — 本机执行 `ssh -L 3018:127.0.0.1:3018 用户@服务器IP`,浏览器开 `http://127.0.0.1:3018` 设密码

> 详细步骤与重置密码见 [docs/deploy-password.md](./docs/deploy-password.md)。设完密码后改密码走页面 UI(`设置 → 修改密码`)。

---

## 🏗️ 技术栈

| 层           | 选型                                                                                              |
| :----------- | :------------------------------------------------------------------------------------------------ |
| **后端**     | FastAPI · Pydantic v2 · APScheduler · sse-starlette                                               |
| **数据**     | Polars(计算)· DuckDB(查询)· Parquet(存储)                                                         |
| **回测**     | vectorbt(全项目唯一 pandas 边界)                                                                  |
| **数据源**   | [TickFlow](https://tickflow.org/auth/register?ref=V3KDKGXPEA) 官方 SDK 、其他数据源后续迭代实装   |
| **AI**(可选) | OpenAI 兼容接口(DeepSeek / 通义 / Ollama 等)                                                      |
| **前端**     | React 18 · Vite · TypeScript · Tailwind · Tanstack Query · Lightweight Charts · ECharts · dnd-kit |
| **部署**     | Docker 两阶段构建,前端 dist 拷进后端镜像,**单容器**                                               |

---

## 🗺️ 路线图

| Phase  | 内容                                                               | 状态 |
| :----- | :----------------------------------------------------------------- | :--- |
| 0-1    | 仓库骨架 · FastAPI 壳 · 能力探测 · K 线同步与分析页                | ✅   |
| 2-3    | Polars enriched 流水线 · Screener · vectorbt 回测(T+1/手续费/止损) | ✅   |
| 4-5    | 监控引擎 · 四类监控规则 · 实时 SSE 推送 · 持久化记录               | ✅   |
| 6      | 个股分析(专用日 K + 9 类关键价位 + AI 四维分析)                    | ✅   |
| **v2** | Webhook 推送(QMT/掘金下单)· 板块异动 · 早晚报 · 更多扩展           | 🚧   |

---

## 📚 文档与贡献

- [docs/strategy-guide.md](./docs/strategy-guide.md) —— 策略开发指南(AI 生成与手写规范)
- [docs/](./docs) —— 策略构建步骤、示例

欢迎 Issue 和 PR。新增内置策略:在 `backend/app/strategy/builtin/` 参照现有文件实现 `StrategyDef`,引擎自动发现。

---

## ⚠️ 免责声明

本项目仅供**学习与量化研究**,**不构成任何投资建议**。回测结果不代表未来收益。A 股有风险,入市需谨慎。数据准确性以数据源 TickFlow 官方为准。

## 📄 License

[MIT](./LICENSE) © tickflow-stock-panel contributors · 本项目依赖 [TickFlow](https://tickflow.org/auth/register?ref=V3KDKGXPEA) 提供数据服务,使用前请遵守其服务条款。

## 社区

本开源项目已链接并认可 [LINUX DO 社区](https://linux.do)。

---

## 🧪 本地开发与数据源（开发团队附录）

> 本节面向**接手开发者 / AI Agent**，描述项目当前的数据源架构、本地启动命令与局域网访问验证。普通用户请按上面的「🚀 快速开始」走 `dev.sh` / `docker compose` 即可，无需阅读本节。

### 📡 数据源架构

本项目原本**只依赖 TickFlow 付费 SDK**。从 2026-07-02 起，项目已实现 **`FQuantProvider v2`**，通过 `data_providers` 抽象层**直接连接底层本地数据源**，逐步摆脱对 TickFlow 付费接口的依赖：

| 上游源 | 协议 | 用途 | 配置 |
|--------|------|------|------|
| **fstore PostgreSQL** | psycopg v3 | 标的列表 / 财务报表 / 复权事件 / 分钟级备份 | `FSTORE_DATABASE_HOST/PORT/USER/PASSWORD/NAME`（默认 `pve.wf:5432/fstore`） |
| **engine-data** | HTTP GET | 日 K 主源（`wide`） / 分钟 / xdxr / trans | `http://192.168.5.99:8099` |
| **moneyflow** | HTTP GET | 资金流日 / 资金流分钟 | `http://pve.wf:8090`（上次测试 502，已自动降级） |
| **tdx-api（可选）** | HTTP GET | realtime quote 主源；未配置时回退 fstore 快照 | `FQUANT_TDX_API_BASE` / `DSA_TDX_API_BASE_URL` / `TDX_API_BASE_URL` |

### 🔁 Provider 切换

通过 `DATA_PROVIDER` 环境变量或 `/api/settings/preferences/data-provider` 在两个 provider 之间切换；环境变量优先级最高。

| Provider | 数据来源 | capabilities | 默认 | 切换方式 |
|----------|---------|--------------|------|----------|
| `tickflow`（默认） | TickFlow SDK（付费） | 全部 7 项 | ✅ | 默认或 settings API |
| `fquant` | fstore PG + engine-data + moneyflow + 可选 tdx-api | 日 K / 复权 / 分钟 / 财务 / realtime / universes；**depth 缺口** | ❌ | `DATA_PROVIDER=fquant` 或 settings API |

### ✅ Service 层解耦状态

**7 个 service 已切到 provider 抽象层**，按统一模式（`_get_data_provider()` 工厂 + `registry.get_active_provider_name()` + `registry.get_provider()`）替换 SDK 调用，业务公开 API 零修改：

| Service | 改动 | 验证 |
|---------|------|------|
| `kline_sync.py` | 试点文件 | 250 行日 K ✅ |
| `instrument_sync.py` | 标准解耦 | 5857 条标的 ✅ |
| `quote_service.py` | tickflow 回归；fquant 走 tdx-api / fstore 快照 | ✅ |
| `financial_sync.py` | 财务报表走 fstore | 22101 行利润表 ✅ |
| `index_sync.py` | universes 走 provider `get_by_universes()` | fquant live 验证 ✅ |
| `watchlist.py` | realtime 走 provider；fquant 走本地源 fallback | ✅ |
| `depth_service.py` | 能力检查模式：fquant 直接降级返回空，tickflow 保留 SDK | ✅ |

### ⚠️ 已知缺口

- **depth（5 档盘口）当前缺口**：FQuantProvider 目前不暴露 depth capability，`depth_service.py` 已做能力门控降级，fquant 模式下返回空列表
- **realtime 已接入**：不调用 `../fquant` HTTP API；优先可选 `tdx-api` `/api/quote`，否则回退 fstore `daily_markets` 最新快照
- **universes 已接入**：provider 协议已新增 `get_by_universes()`；fquant 走 fstore `chengfen_gu` + `base_infos`，TickFlow 走 SDK 兼容路径

### 🚀 本地启动（DATA_PROVIDER=fquant）

```bash
cd backend
uv sync
export DATA_PROVIDER=fquant
export FSTORE_DATABASE_PASSWORD=$(grep FSTORE_DATABASE_PASSWORD /Users/wf2311/Projects/wf2311/fm/fquant/.env | cut -d= -f2)
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动后可访问：

- 本机：`http://127.0.0.1:8000/health`
- 局域网：`http://<本机 LAN IP>:8000/health`

### 🌐 局域网访问

backend 默认 `--host 0.0.0.0` 已支持所有网卡监听，**无需修改**。需在 macOS 防火墙放行 8000 端口：

```bash
# 如系统防火墙阻拦，自行添加：
# 系统设置 → 网络 → 防火墙 → 允许以下应用接受传入连接
# 或临时关闭防火墙测试（不推荐生产）
```

获取本机 LAN IP：

```bash
ipconfig getifaddr en0   # Wi-Fi
# 或
ipconfig getifaddr en1   # 有线/USB 网卡
```

### 📚 详细进度文档

完整的 FQuant 接入进度、架构设计、风险与注意事项请阅：

- **`backend/docs/FQUANT_INTEGRATION_PROGRESS.md`** — 团队状态文档（**权威进度源**）
- `backend/docs/FQUANT_PROVIDER_DESIGN.md` — 846 行设计稿（三源实测 + 架构）
- `backend/docs/FQUANT_PROVIDER.md` — 旧 PoC 说明（已被 v2 覆盖，仅供回溯）

新增 service 文件 / 修改 provider 时**务必**先读 `FQUANT_INTEGRATION_PROGRESS.md` 第 8 节「关键决策」与第 9 节「风险与注意事项」，避免破坏已对齐的架构约束。
