<div align="center">

# 📈 本地量化工作台

**自托管、以本地数据源为主的「选股 + 监控 + 回测 + 复盘」量化工作台**

**面向个人散户与量化爱好者而生**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-≥3.11-blue.svg)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-18-61dafb.svg)](https://react.dev/)
[![Data: fquant_local](https://img.shields.io/badge/Data-fquant__local-00b386.svg)](./README.md#-本地开发与数据源开发团队附录)
[![Deploy: Docker](https://img.shields.io/badge/Deploy-Docker-2496ed.svg)](./Dockerfile)
[![GitHub stars](https://img.shields.io/github/stars/shy3130/tickflow-stock-panel?style=social)](https://github.com/shy3130/tickflow-stock-panel/stargazers)

</div>

<div align="center">

**[快速开始](#-快速开始)** · **[核心功能](#-核心功能)** · **[配置](#️-配置)** · **[路线图](#-路线图)**

</div>

- 🏠 **本地数据优先** — 默认 `DATA_PROVIDER=fquant_local`,走本地 DuckDB (`fstore*.duckdb` + `tdx*.duckdb`,含港股拆分库)
- 🏠 **自托管零运维** — Docker 单容器部署,数据完全掌握在自己手里
- 🔍 **多工具工作台** — 选股(20 内置策略)+ 实时监控 + 向量化回测 + 组合优化 + 交易复盘
- 🤖 **多 AI 配置** — 支持 OpenAI 兼容接口 / ACP / Codex CLI profile,可按功能选择
- 🔌 **自由扩展** — 自有量化项目数据,与内置数据同台分析
- 🇨🇳 **A 股为主,港股 P1 已接入** — A 股全功能;港股支持单股行情/K线/分析的第一阶段能力
- 📣 **多通道推送** — 飞书 / 钉钉 / 企微 / MeoW webhook,用于监控告警与复盘推送



项目通过 `data_providers` 抽象层接入本地与远端数据源。当前默认本地源为 `fquant_local`。**明确不做**:不对标同花顺 / 通达信,不内置「AI 荐股 / 涨停预测」。

> ⚠️ 项目仍保留历史上的 TickFlow 命名与部分兼容痕迹,但新开发默认面向 `fquant_local` / `fquant` provider。资金流、概念/行业、ETF、财务等能力以本地数据与 fstore 覆盖为准。

 
> 有更多稳定免费数据源推荐,或者提交建议/意见的大佬可以邮件到 415333856@qq.com,q群 109338242


觉得有用可以点个 Star，蟹蟹 🌹

---

## 🎯 项目定位

**面向个人散户与量化爱好者的分析工作台**,聚焦「**选股 + 监控 + 回测 + 交易复盘**」等场景。LLM 可辅助生成策略、复盘市场、分析个股和财务,但核心计算与数据链路尽量本地化、可审计。

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
cp .env.example .env       # 按需补 DATA_PROVIDER / DuckDB 路径 / AI
make start-local           # 默认 DATA_PROVIDER=fquant_local; 或直接 ./dev.sh
```

自动检查 / 下载依赖、释放端口、同时起前后端,Ctrl-C 一并关闭。默认:

- 后端 → <http://localhost:3018> · 前端 → <http://localhost:3011>
- 局域网访问 → `http://<本机 LAN IP>:3011/`
- 自定义端口:`BACKEND_PORT=8000 FRONTEND_PORT=5173 ./dev.sh`

> `Makefile` 默认 `DATA_PROVIDER=fquant_local`。DuckDB 路径不填时使用 `/Volumes/WD1/*.duckdb` 默认挂载。

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
cd backend && uv sync --extra dev
DATA_PROVIDER=fquant_local uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 3018

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

1. **设置 → 系统/数据源** → 确认当前 provider capability,默认应为 `fquant_local`
2. **设置 → 数据** → 按需跑盘后管道 / enriched 重建;本地模式优先使用 TDX `wide/` 与 enriched 分区
3. **自选**页加标的 → **选股**页点策略卡片扫描 / 配自定义信号
4. **回测**页选策略 + 区间,或进 **组合优化** 页为 A 股/ETF 计算配置权重
5. **交易复盘**上传券商流水,生成 FIFO 台账、行为诊断与基准超额
6. **监控中心**配规则(策略 / 个股信号 / 价格 / 异动),盘中实时弹窗 + 持久化记录 + webhook 推送

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

### 🧪 回测与组合优化(Backtest / Optimizer)

基于 vectorbt:**三种模式**(个股 / 策略组合 / 自由信号组合),真实约束(T+1 · 手续费 · 滑点 · 止损 · 最大持仓天数),组合管理(最大持仓 · 敞口 · 等权 / 自定义仓位)。SSE 流式进度支持切页重连,输出净值曲线 · 夏普 · 最大回撤 · 胜率 · 交易明细。

**组合优化器**(`/optimizer`) 已接入:

- A 股 + ETF 日线收益矩阵,港股/无数据标的自动 dropped 提示
- 6 种权重方法:等权 · 等波动 · 风险平价 · 均值方差 · 最大分散 · 动量加权
- 权重表 + 环形图 + 年化波动 / 分散度统计
- 可从策略池一键导入命中标的再做组合配置

### 📡 监控中心(Monitor)

统一规则引擎,一个页面管理**四类监控**(策略 · 个股信号 · 价格涨跌 · 全市场异动):

- 多条件 AND/OR + 冷却期去重 + 严重级别(info/warn/critical)
- 多入口配置:监控中心新建 / 个股详情页「加监控」/ 策略卡片一键开启
- 命中后右下角弹窗(可配声效)+ 持久化到 `alerts.jsonl`,菜单未读徽标
- **触发记录详情**:每条记录展示命中的具体条件(如 `RSI>80`)与当前价位,一眼看清为何触发
- **多通道 Webhook 推送**:飞书 / 钉钉 / 企微 / MeoW;可配置默认推送,新建规则自动预填

### 📈 个股分析(Beta)

以「行情 + 关键价位」为主体的单标的决策页:

- **专用日 K 图表**:主图 + 成交量 + 滑块,默认近 6 个月
- **9 类关键价位**(纯函数实时计算,毫秒级):压力支撑 · 成交密集区 · 枢轴点 · 前高前低 · Keltner 通道 · ATR 止损 · 缺口位 · 斐波那契 · 整数关口
- **AI 四维分析**:技术 / 基本面 / 财务 / 消息面流式生成,实战派交易员视角
- **港股 P1**:单股 K 线、实时行情和分析链路已按市场类型区分;港股当前标注未复权,涨跌停类指标不参与

### 🧾 交易复盘(Trade Journal)

- 上传券商成交流水 / 同花顺投资账本导出,解析 A 股与港股代码
- FIFO 配对生成 position-cycle 台账,支持已清仓与持仓中交易
- 行为诊断:处置效应 · 过度交易 · 追涨买入 · 浮亏加仓
- 基准超额与追涨位置诊断;本地无日 K 或港股覆盖不足会在 warning 中明确提示
- 原始 fills 与报告分离存储,AI 方法论上下文只作为响应展示,不污染 ledger

### 💰 财务与 AI 分析

- fstore 财务表 + 东方财富 forecast fallback,覆盖利润表 / 资产负债表 / 现金流 / 指标 / 业绩预告
- 个股、财务、复盘、策略生成等入口支持 AI 辅助
- AI 配置支持多 profile:OpenAI 兼容接口、ACP(Hermes 等)、Codex CLI;可设置全局默认并按功能选择

### 🧰 数据与扩展

- **本地/远端多源 provider**:`fquant_local` 默认,`fquant` 可选;日 K / ETF / 指数 / 分钟 / 财务 / 实时行情按 capability 降级
- **🔌 第三方接入(重点)**:Tushare 等 HTTP 定时拉取 · CSV / Excel 上传 · JSON 写入,自动 schema 发现 + 符号归一,页面可视化配置,**可与自有量化项目数据并入 DuckDB 同台分析**
- **盘后定时管道**:APScheduler 15:30 CST 自动拉日 K + 重算 enriched + 跑监控
- **本地 enriched 管道**:本地模式下 raw mirror 禁写,以 enriched 分区作为查询和选股主表;ETF 独立日线分区可用于追涨/组合优化

---

## ⚙️ 配置

所有配置从根目录 `.env` 读取(复制 `.env.example` 开始),也可在面板 **设置** 页修改。

### 数据源

```ini
DATA_PROVIDER=fquant_local     # 默认:fquant_local; 可选:fquant
FQUANT_FSTORE_DUCKDB_PATH=/Volumes/WD1/fstore-web.duckdb
FQUANT_FSTORE_MARKETS_DUCKDB_PATH=/Volumes/WD1/fstore-markets-web.duckdb
FQUANT_FSTORE_KLINES_DUCKDB_PATH=/Volumes/WD1/fstore-klines-web.duckdb
FQUANT_FSTORE_MINUTES_DUCKDB_PATH=/Volumes/WD1/fstore-minutes-web.duckdb
FQUANT_TDX_DUCKDB_PATH=/Volumes/WD1/tdx.duckdb
FQUANT_TDX_MINUTES_DUCKDB_PATH=/Volumes/WD1/tdx-minutes.duckdb
FQUANT_TDX_TRANS_DUCKDB_PATH=/Volumes/WD1/tdx-trans.duckdb
FQUANT_TDX_HK_DUCKDB_PATH=/Volumes/WD1/tdx-hk-web.duckdb
FQUANT_TDX_HK_MINUTES_DUCKDB_PATH=/Volumes/WD1/tdx-hkminutes-web.duckdb
FQUANT_TDX_HK_TRANS_DUCKDB_PATH=/Volumes/WD1/tdx-hktrans-web.duckdb
```

当前项目以 provider capability 判断功能可用性,不再以 TickFlow 订阅档位作为默认门槛。`fquant_local` 主路径:

- 日 K / ETF / 指数:TDX DuckDB + 本地 Parquet/enriched
- 标的 / 财务 / ETF 备份:fstore DuckDB
- 实时行情:fstore markets DuckDB `daily_markets` 最新快照
- 5 档盘口 depth:当前仍是缺口,相关功能会能力门控降级

`DATA_PROVIDER` 环境变量优先级最高;未设置时读取设置页偏好,未知值会回落到 `fquant_local`。

### AI(可选)

用于自然语言生成策略、个股/财务/市场复盘。**所有配置留空即跳过**,不影响核心功能。支持多 profile:

```ini
AI_PROVIDER=openai_compat              # openai_compat | acp | codex_cli
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=                            # 留空 = 关闭 AI
AI_MODEL=deepseek-chat
AI_CODEX_COMMAND=codex                 # codex_cli provider 使用
AI_DAILY_TOKEN_BUDGET=500000           # 每日 token 预算上限
```

页面设置支持新增多条 AI 配置、设默认 profile,并在部分功能入口选择本次使用的 profile。

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
| **数据源**   | `data_providers` 抽象层 · fquant_local/fquant(本地 DuckDB)                                      |
| **AI**(可选) | 多 profile · OpenAI 兼容接口 · ACP · Codex CLI                                                     |
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
| 7      | 本地 provider / 多 AI profile / 交易复盘 / 组合优化器 / 港股 P1     | ✅   |
| **v2** | 港股批量 enrich · depth 能力补齐 · 影子账户 · 更多扩展              | 🚧   |

---

## 📚 文档与贡献

- [docs/strategy-guide.md](./docs/strategy-guide.md) —— 策略开发指南(AI 生成与手写规范)
- [docs/](./docs) —— 策略构建步骤、示例

欢迎 Issue 和 PR。新增内置策略:在 `backend/app/strategy/builtin/` 参照现有文件实现 `StrategyDef`,引擎自动发现。

---

## ⚠️ 免责声明

本项目仅供**学习与量化研究**,**不构成任何投资建议**。回测结果不代表未来收益。A 股 / 港股 / ETF 均有风险,入市需谨慎。数据准确性取决于当前启用的 provider 与本地数据新鲜度。

## 📄 License

[MIT](./LICENSE) © tickflow-stock-panel contributors

## 社区

本开源项目已链接并认可 [LINUX DO 社区](https://linux.do)。

---

## 🧪 本地开发与数据源（开发团队附录）

> 本节面向**接手开发者 / AI Agent**，描述项目当前的数据源架构、本地启动命令与局域网访问验证。普通用户请按上面的「🚀 快速开始」走 `dev.sh` / `docker compose` 即可，无需阅读本节。

### 📡 数据源架构

本项目原本只依赖 TickFlow SDK。当前主线已切到 **`FQuantProvider v2` + `fquant_local` 默认本地模式**,通过 `data_providers` 抽象层只读本地 DuckDB：

| 上游源 | 协议 | 用途 | 配置 |
|--------|------|------|------|
| **fstore DuckDB** | DuckDB read-only | 标的列表 / 财务报表 / 复权事件 / universes / 小表 | `FQUANT_FSTORE_DUCKDB_PATH`（默认 `/Volumes/WD1/fstore-web.duckdb`） |
| **fstore markets DuckDB** | DuckDB read-only | realtime 快照 / 每日行情 | `FQUANT_FSTORE_MARKETS_DUCKDB_PATH`（默认 `/Volumes/WD1/fstore-markets-web.duckdb`） |
| **fstore klines DuckDB** | DuckDB read-only | fstore K 线兼容表 | `FQUANT_FSTORE_KLINES_DUCKDB_PATH`（默认 `/Volumes/WD1/fstore-klines-web.duckdb`） |
| **fstore minutes DuckDB** | DuckDB read-only | fstore 分钟 K 线 | `FQUANT_FSTORE_MINUTES_DUCKDB_PATH`（默认 `/Volumes/WD1/fstore-minutes-web.duckdb`） |
| **TDX DuckDB** | DuckDB read-only | 日 K wide/day / xdxr / 日级资金流 | `FQUANT_TDX_DUCKDB_PATH`（默认 `/Volumes/WD1/tdx.duckdb`） |
| **TDX minutes DuckDB** | DuckDB read-only | 分钟 K | `FQUANT_TDX_MINUTES_DUCKDB_PATH`（默认 `/Volumes/WD1/tdx-minutes.duckdb`） |
| **TDX trans DuckDB** | DuckDB read-only | 逐笔成交 | `FQUANT_TDX_TRANS_DUCKDB_PATH`（默认 `/Volumes/WD1/tdx-trans.duckdb`） |
| **TDX HK DuckDB** | DuckDB read-only | 港股日 K / 多周期 K | `FQUANT_TDX_HK_DUCKDB_PATH`（默认 `/Volumes/WD1/tdx-hk-web.duckdb`） |
| **TDX HK minutes DuckDB** | DuckDB read-only | 港股分钟 K | `FQUANT_TDX_HK_MINUTES_DUCKDB_PATH`（默认 `/Volumes/WD1/tdx-hkminutes-web.duckdb`） |
| **TDX HK trans DuckDB** | DuckDB read-only | 港股逐笔成交 | `FQUANT_TDX_HK_TRANS_DUCKDB_PATH`（默认 `/Volumes/WD1/tdx-hktrans-web.duckdb`） |

### 🔁 Provider 切换

通过 `DATA_PROVIDER` 环境变量或 `/api/settings/preferences/data-provider` 在 provider 之间切换；环境变量优先级最高。

| Provider | 数据来源 | capabilities | 默认 | 切换方式 |
|----------|---------|--------------|------|----------|
| `fquant` | 本地 DuckDB，保留 provider 名称兼容 | 日 K / 复权 / 分钟 / 财务 / realtime / universes；**depth 缺口** | ❌ | `DATA_PROVIDER=fquant` 或 settings API |
| `fquant_local` | 本地 DuckDB，source tag 保留 `fquant_local` | 日 K / ETF / 指数 / 分钟 / 复权 / 财务 / realtime / universes；扩展逐笔/日级资金流；**stock raw mirror 禁写**；**depth 缺口** | ✅ | 默认、`DATA_PROVIDER=fquant_local` 或 settings API |

### ✅ Service 层解耦状态

**7 个 service 已切到 provider 抽象层**，按统一模式（`_get_data_provider()` 工厂 + `registry.get_active_provider_name()` + `registry.get_provider()`）替换 SDK 调用，业务公开 API 零修改：

| Service | 改动 | 验证 |
|---------|------|------|
| `kline_sync.py` | 试点文件 | 250 行日 K ✅ |
| `instrument_sync.py` | 标准解耦 | 5857 条标的 ✅ |
| `quote_service.py` | realtime 走 provider；fquant_local 走 `fstore-markets-web.duckdb.daily_markets` 快照 | ✅ |
| `financial_sync.py` | 财务报表走 fstore | 22101 行利润表 ✅ |
| `index_sync.py` | universes 走 provider `get_by_universes()` | fquant live 验证 ✅ |
| `watchlist.py` | realtime 走 provider；fquant 走本地源 fallback | ✅ |
| `depth_service.py` | 能力检查模式：本地/fquant provider 无 depth 时降级返回空 | ✅ |

### ⚠️ 已知缺口

- **depth（5 档盘口）当前缺口**：FQuantProvider 目前不暴露 depth capability，`depth_service.py` 已做能力门控降级，本地/fquant 模式下返回空列表
- **realtime 已接入**：不调用 `../fquant` HTTP API / tdx-api / sina / tencent；只读 `fstore-markets-web.duckdb.daily_markets` 最新快照
- **universes 已接入**：provider 协议已新增 `get_by_universes()`；fquant/fquant_local 走 fstore `chengfen_gu` + `base_infos`
- **港股 P1 限制**：单股路径可用;批量 enrich、回测/筛选全链路和复权口径仍按后续计划推进

### 🚀 本地启动（DATA_PROVIDER=fquant_local）

```bash
make start-local
```

启动后可访问：

- 本机：`http://127.0.0.1:3011/` / `http://127.0.0.1:3018/health`
- 局域网：`http://<本机 LAN IP>:3011/`

### 🌐 局域网访问

`dev.sh` / `make start-local` 默认前后端都用 `--host 0.0.0.0`,已支持所有网卡监听。需在 macOS 防火墙放行 3011/3018 端口：

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
- `docs/hk-us-stock-expansion-assessment.md` — 港股/美股扩展可行性与实测矩阵

新增 service 文件 / 修改 provider 时**务必**先读 `FQUANT_INTEGRATION_PROGRESS.md` 第 8 节「关键决策」与第 9 节「风险与注意事项」，避免破坏已对齐的架构约束。
