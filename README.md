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

**[快速开始](#-快速开始)** · **[功能指南](./docs/FEATURE_GUIDE.md)** · **[核心功能](#-核心功能)** · **[配置](#️-配置)** · **[路线图](#-路线图)**

</div>

- 🏠 **本地数据优先** — 默认 `DATA_PROVIDER=fquant_local`,走本地 DuckDB (`fstore*.duckdb` + `tdx*.duckdb`,含港股拆分库)
- 🏠 **自托管零运维** — Docker 单容器部署,数据完全掌握在自己手里
- 🔍 **多工具工作台** — 选股(20 内置策略)+ 实时监控 + 向量化回测 + 组合优化 + 交易复盘
- 🤖 **多 AI 配置** — 支持 OpenAI 兼容接口 / ACP / Codex CLI profile，可按功能选择；已保存 profile 可做不经过 fallback 的最小连通性测试
- 🔌 **自由扩展** — 自有量化项目数据,与内置数据同台分析
- 🇨🇳 **A 股为主,港股 P1 已接入** — A 股全功能;港股支持单股行情/K线/分析的第一阶段能力
- 📣 **多通道推送** — 飞书 / 钉钉 / 企微 / MeoW webhook + PushPlus,用于监控告警与复盘推送



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
| Node                               | ≥ 20   | [nodejs.org](https://nodejs.org/)；Pi Agent 试点需 ≥ 22.19 |
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

**Pi Agent Harness 运行时试点（可选，仅自由 Agent）**：默认仍使用 Python Agent loop。源码开发环境可让 `/api/agent/*` 改走 Pi Agent Harness sidecar；其它 AI 入口、Docker 和 PyInstaller 不变。试点只支持 `openai_compat` profile，且每次 attempt 不做隐式 runtime fallback。

```bash
make start-pi
```

首次运行或 `pi-agent-worker/package*.json` 变化时，Make 会自动执行 `npm ci --ignore-scripts`；也可单独运行 `make pi-deps` 预装依赖。

Node 版本须 ≥ 22.19。Python 进程继续执行 13 个只读业务工具和持有业务状态；Node sidecar 只负责模型会话循环。配置项及验收矩阵见 [`backend/docs/PI_AGENT_PILOT_PLAN.md`](./backend/docs/PI_AGENT_PILOT_PLAN.md)。

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

### 🔎 个股详情·资金行为

- 在个股详情打开「分时」后，可展开 **资金行为**：逐笔成交散点图按可靠的单笔金额分档，并叠加分时价/均价；最近 6 个交易日展示超大/大/中/小单与主力净额。
- 逐笔 `direction` 编码尚无上游统一语义，当前**刻意不展示**主动/被动买卖四维或换手度，不把未验证字段伪装成资金方向；分时基准与资金流分档缺失均明确标记，资金流展示 API 与行级来源。

### 🧾 交易复盘(Trade Journal)

- 上传券商成交流水 / 同花顺投资账本导出,解析 A 股与港股代码
- 默认优先同级 `../fhold/fhold-cli`（开发工作区），可用 `FHOLD_CLI` 显式覆盖；从 `../fhold` 通过 `fhold-cli tx snapshot --format json`（仅本地模式）获得完整一致快照后只读预览并确认追加；无法证明一致性时拒绝导入；按原始交易 ID 去重，不直读数据库、不写回 fhold，也不写入交易事件流
- FIFO 配对生成 position-cycle 台账,支持已清仓与持仓中交易
- 行为诊断:处置效应 · 过度交易 · 追涨买入 · 浮亏加仓
- 基准超额与追涨位置诊断;本地无日 K 或港股覆盖不足会在 warning 中明确提示
- 原始 fills 与报告分离存储,AI 方法论上下文只作为响应展示,不污染 ledger

### 🧭 交易与复盘(Trading & Review)

- **单笔交易生命周期**：事件流驱动 `计划中 → 建仓中 → 持仓中 → 已平仓`，零成交计划可进入 `已作废`。`fill` 支持分批成交和显式收口；`trim` 只在建仓中缩减未成交计划，`add` 可调大计划并从持仓中重开建仓，但都不伪造仓位；事件 append-only 持久化（`trade_events.jsonl`），服务端重算成本均价与已实现盈亏，平仓资金按 `tradeId` 幂等结转
- **组合风险透视**：`GET /api/trading/portfolio/risk` 只读 `建仓中/持仓中` 的真实敞口与 canonical 日 K，在后端计算组合波动、最大回撤、相关性集中、有效持仓数和风险贡献；缺持仓/共同样本不足时返回明确状态与 warning，前端不重算
- **真实券商持仓**：`GET /api/trading/portfolio` 只读调用 `fhold-cli --format json` 聚合 `../fhold` 的账户与持仓；CLI/服务不可用时 `fhold.available=false`，不阻断生命周期快照
- **决策审计**：任何买卖动作（含门禁未通过仍确认的绕行）都写入 append-only 审计流（`decision_audit.jsonl`），永不清理；审计断链即告警
- **机械红旗**:在事件流+审计流上实时检出放宽止损、亏损加仓、绕过门禁、审计断链、期限超限（对照策略声明 horizon）、仓位超限（对照账户/策略上限）与门禁膨胀（规则清单>15 条全局提示）；赚钱的违规也照记。设置 `TRADING_RED_FLAG_WEBHOOK_URL` 后每条新红旗去重推送一次
- **策略内核治理**:策略 profile 声明失效信号/风险/期限，可选策略坐标卡 family（价值/成长/趋势/事件/短周期/套利/混合，混合需声明裁判归属）与 playbook（scope/entry/exit）；机械体检 7 项检查（完整性/节奏/期限漂移/剧本声明/混合冲突/自称与行为冲突/提案治理），`validate?ai=true` 追加 AI 深度体检（对照 7 项结构不变量）；回测交易带 `cause_tag`，变更提案必须有反证条件并走人工审批状态机，疑似亏损后放宽规则的提案自动打 `relaxationAfterLoss` 警示
- **结构化计划检查（默认关闭）**:计划台可对“已保存的单条计划”运行 Stage1 市场诊断 → 程序门禁 → Stage2 计划审查。程序门禁只可保持或降级，AI 不生成订单、方向、建议价格或执行动作；结果含可审计决策链并支持 JSON/Markdown 导出。页面中的“输入完整，可进入审查”仅表示数据与前置条件充分，不代表建议交易
- **盘后状态驱动归因（L0/L1/L2）**:`POST /api/trading/review/auto-run` 或开启 `tradingAutoReview` 后每交易日 16:45 自动跑——无新红旗/新平仓时 L0 零 AI 调用，有候选时 L1 只对涉及单笔归因且按事件数去重，L2 为用户手动全量；AI 未配置按 `blocked_by_dependency` 降级
- **统一失败语义**:`AppError` + 7 个标准错误码(`data_incomplete / stale_input / blocked_by_dependency / no_change / kernel_not_ready / ai_output_invalid / ai_provider_error`),API 返回 HTTP 422 + `{"code","detail"}`,前端可据此区分数据前置条件、模型输出无效、provider 故障、需介入与无变化
- **Webhook / PushPlus**:监控规则命中按已配置渠道推送；纪律红旗按环境变量推送；每日复盘可选飞书、钉钉、企微、MeoW 或 PushPlus。PushPlus Token 只保存在 `secrets.json` 并通过设置页掩码展示。所有外部推送失败均只记日志，不阻断报告、告警、事件或审计落盘
- 前端 `/trading` 提供持仓、单笔生命周期、计划台和账户四个可用页签，并展示后端组合风险透视；计划台可显式开启结构化检查并选择 AI profile；`/review` 增加纪律红旗；设置页提供策略提案、策略体检、复盘通知和单 profile 连接测试

> 完整机制设计与移植计划见 [`backend/docs/YMOS_PORTING_PLAN.md`](./backend/docs/YMOS_PORTING_PLAN.md)(来源:`fm/YMOS` 投资操作系统 V4)。

### 💰 财务与 AI 分析

- fstore 财务表 + 东方财富 forecast fallback,覆盖利润表 / 资产负债表 / 现金流 / 指标 / 业绩预告
- 个股、财务、复盘、策略生成等入口支持 AI 辅助
- AI 配置支持多 profile:OpenAI 兼容接口、ACP(Hermes 等)、Codex CLI;可设置全局默认并按功能选择

### 🧰 数据与扩展

- **本地/远端多源 provider**:`fquant_local` 默认,`fquant` 可选;日 K / ETF / 指数 / 分钟 / 财务 / 实时行情按 capability 降级
- **🔌 第三方接入(重点)**:Tushare 等 HTTP 定时拉取 · CSV / Excel 上传 · JSON 写入,自动 schema 发现 + 符号归一,页面可视化配置,**可与自有量化项目数据并入 DuckDB 同台分析**
- **盘后定时管道**:APScheduler 15:30 CST 自动拉日 K + 重算 enriched + 跑监控；本地 enriched 仅在 freshness/覆盖率校验后发布 canonical 可见水位
- **本地 enriched 管道**:本地模式下 raw mirror 禁写,以 enriched 分区作为查询和选股主表；ETF 独立日线分区可用于追涨/组合优化；看板、回测与复盘依赖的关键指数会自动补齐 canonical 长历史

---

## ⚙️ 配置

所有配置从根目录 `.env` 读取(复制 `.env.example` 开始),也可在面板 **设置** 页修改。

### 数据源

```ini
DATA_PROVIDER=fquant_local     # 默认:fquant_local; 可选:fquant
FQUANT_FSTORE_DUCKDB_PATH=/Volumes/WD1/duckdb/fstore.duckdb
FQUANT_FSTORE_MARKETS_DUCKDB_PATH=/Volumes/WD1/duckdb/fstore-markets.duckdb
FQUANT_FSTORE_KLINES_DUCKDB_PATH=/Volumes/WD1/duckdb/fstore-klines.duckdb
FQUANT_FSTORE_EXTENDED_DUCKDB_PATH=/Volumes/WD1/duckdb/fstore-extended.duckdb
FQUANT_TDX_DUCKDB_PATH=/Volumes/WD1/duckdb/tdx.duckdb
FQUANT_SNAPSHOT_ROOT_CATALOG=/Volumes/WD1/duckdb/snapshots/catalog
# A 股 minutes/trans 按交易日从 staged catalog 解析;所有 root 默认根 /Volumes/WD1/duckdb
FQUANT_SNAPSHOT_ROOT_ENGINE_A=/Volumes/WD1/duckdb/snapshots/engine-a
FQUANT_SNAPSHOT_ROOT_ENGINE_A_PRELIMINARY=/Volumes/WD1/duckdb/snapshots/engine-a-preliminary
FQUANT_SNAPSHOT_ROOT_ENGINE_A_MINUTES_ARCHIVE=/Volumes/WD1/duckdb/snapshots/engine-a-minutes-archive
FQUANT_SNAPSHOT_ROOT_ENGINE_A_TRANS_ARCHIVE=/Volumes/WD1/duckdb/snapshots/engine-a-trans-archive
FQUANT_SNAPSHOT_ROOT_FSTORE_EXTENDED=/Volumes/WD1/duckdb/snapshots/fstore-extended
FQUANT_SNAPSHOT_ROOT_ENGINE_A_MONEYFLOW_MINUTE=/Volumes/WD1/duckdb/snapshots/engine-a-moneyflow-minute
FQUANT_SNAPSHOT_ROOT_ENGINE_A_CALLAUCTION=/Volumes/WD1/duckdb/snapshots/engine-a-callauction
TICKFLOW_CANONICAL_HISTORY_ROOT=/Volumes/WD1/duckdb/snapshots/tickflow-canonical-history
FQUANT_TDX_HK_DUCKDB_PATH=/Volumes/WD1/duckdb/tdx-hk.duckdb
FQUANT_TDX_HK_MINUTES_DUCKDB_PATH=/Volumes/WD1/duckdb/tdx-hkminutes.duckdb
FQUANT_TDX_HK_TRANS_DUCKDB_PATH=/Volumes/WD1/duckdb/tdx-hktrans.duckdb
```

当前项目以 provider capability 判断功能可用性,不再以 TickFlow 订阅档位作为默认门槛。`fquant_local` 主路径:

- 日 K / ETF / 指数:TDX DuckDB + 本地 Parquet/enriched
- 标的 / 财务 / ETF 备份:fstore DuckDB
- 实时行情:fstore markets DuckDB `daily_markets` 最新快照
- A 股分钟 K / 逐笔：按交易日经 staged catalog 读取 TDX DuckDB 分片
- 筹码、日/分钟资金流、集合竞价：研究页“市场数据”仅读已发布 snapshot；不可用与无数据分开展示
- A 股全历史：数据页手动回填到专用 external generation，成功后原子发布并与本地近期 enriched 合并；不修改用户 `data/`
- 港股：日 K / minutes / trans 可用；本地无港股复权事件与财务报表，能力状态明确降级
- 5 档盘口 depth：当前仍是缺口，相关功能会能力门控降级

`DATA_PROVIDER` 环境变量优先级最高;未设置时读取设置页偏好,未知值会回落到 `fquant_local`。

> **A 股 minutes/trans 读路径**:staged catalog 是前置条件——`require_current` 路由必须为 `stage=preliminary`/`final`，旧 `stage=NULL` 行会被 fail-closed 拒绝（带可行动迁移指引，**不降级 raw**）。发布顺序（先物理 snapshot root 再 catalog 路由）与安全回滚条件见 `AGENTS.md`「catalog/engine 发布顺序」。

### AI(可选)

用于自然语言生成策略、个股/财务/市场复盘，以及用户显式触发的结构化交易计划检查。**所有配置留空即跳过**,不影响核心功能。支持多 profile:

```ini
AI_PROVIDER=openai_compat              # openai_compat | acp | codex_cli
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=                            # 留空 = 关闭 AI
AI_MODEL=deepseek-chat
AI_CODEX_COMMAND=codex                 # codex_cli provider 使用
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
| **fstore DuckDB** | DuckDB read-only | 标的列表 / 财务报表 / 复权事件 / universes / 小表 | `FQUANT_FSTORE_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/fstore.duckdb`，解析为 `snapshots/fstore/<gen>/` 快照） |
| **fstore markets DuckDB** | DuckDB read-only | realtime 快照 / 每日行情 | `FQUANT_FSTORE_MARKETS_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/fstore-markets.duckdb`，解析为 generation 快照） |
| **fstore klines DuckDB** | DuckDB read-only | fstore K 线兼容表 | `FQUANT_FSTORE_KLINES_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/fstore-klines.duckdb`，解析为 generation 快照） |
| **fstore extended DuckDB** | DuckDB read-only | 财务三表 / 复权事件 | `FQUANT_FSTORE_EXTENDED_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/fstore-extended.duckdb`，解析为独立 `snapshots/fstore-extended/<gen>/` 快照） |
| **TDX DuckDB** | DuckDB read-only | 日 K wide/day / xdxr / 日级资金流 | `FQUANT_TDX_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/tdx.duckdb`） |
| **TDX minutes/trans catalog** | DuckDB read-only snapshots | 按交易日解析分钟 K 与逐笔成交分片（staged：preliminary→final，刻意不降级 raw） | `FQUANT_SNAPSHOT_ROOT_CATALOG` + `FQUANT_SNAPSHOT_ROOT_ENGINE_A{,_PRELIMINARY,_MINUTES_ARCHIVE,_TRANS_ARCHIVE}`（默认根 `/Volumes/WD1/duckdb`） |
| **TDX HK DuckDB** | DuckDB read-only | 港股日 K / 多周期 K | `FQUANT_TDX_HK_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/tdx-hk.duckdb`，解析为 engine-hk generation 快照） |
| **TDX HK minutes DuckDB** | DuckDB read-only | 港股分钟 K | `FQUANT_TDX_HK_MINUTES_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/tdx-hkminutes.duckdb`，解析为 engine-hk generation 快照） |
| **TDX HK trans DuckDB** | DuckDB read-only | 港股逐笔成交 | `FQUANT_TDX_HK_TRANS_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/tdx-hktrans.duckdb`，解析为 engine-hk generation 快照） |

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
| `quote_service.py` | realtime 走 provider；fquant_local 走 `fstore-markets.duckdb.daily_markets` generation 快照 | ✅ |
| `financial_sync.py` | 财务报表走 fstore | 22101 行利润表 ✅ |
| `index_sync.py` | universes 走 provider `get_by_universes()` | fquant live 验证 ✅ |
| `watchlist.py` | realtime 走 provider；fquant 走本地源 fallback | ✅ |
| `depth_service.py` | 能力检查模式：本地/fquant provider 无 depth 时降级返回空 | ✅ |

### ⚠️ 已知缺口

- **depth（5 档盘口）当前缺口**：FQuantProvider 目前不暴露 depth capability，`depth_service.py` 已做能力门控降级，本地/fquant 模式下返回空列表
- **realtime 已接入**：不调用 `../fquant` HTTP API / tdx-api / sina / tencent；只读 `fstore-markets.duckdb.daily_markets` generation 快照（最新）
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
- **`backend/docs/UPSTREAM_FEATURE_PORTING.md`** — 上游项目、已移植能力、暂缓/排除项与维护流程总账
- `backend/docs/FQUANT_PROVIDER_DESIGN.md` — 846 行设计稿（三源实测 + 架构）
- `backend/docs/FQUANT_PROVIDER.md` — 旧 PoC 说明（已被 v2 覆盖，仅供回溯）
- `docs/hk-us-stock-expansion-assessment.md` — 港股/美股扩展可行性与实测矩阵

新增 service 文件 / 修改 provider 时**务必**先读 `FQUANT_INTEGRATION_PROGRESS.md` 第 8 节「关键决策」与第 9 节「风险与注意事项」，避免破坏已对齐的架构约束。
