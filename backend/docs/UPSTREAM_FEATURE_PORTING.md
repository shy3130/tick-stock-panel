# 上游功能移植与维护总账

> 日期：2026-08-10；最近增量审计：2026-08-17
> 状态：当前实现账本；来源发现证据见 [`PORTING_SOURCE_REPOSITORY_INVENTORY_2026-08-09.md`](./PORTING_SOURCE_REPOSITORY_INVENTORY_2026-08-09.md)
> 目标：记录“能力从哪里来、在本项目落在哪里、哪些明确不迁移”，防止把外部数据链、自动交易语义或第二套领域模型带回当前架构。

本文只维护功能血缘和最终处置，不替代各专题设计。数据源与发布契约以 `AGENTS.md`、[`FQUANT_INTEGRATION_PROGRESS.md`](./FQUANT_INTEGRATION_PROGRESS.md) 为准；PA_Agent、YMOS 的细节分别以 [`PA_AGENT_PORTING_PLAN.md`](./PA_AGENT_PORTING_PLAN.md)、[`YMOS_PORTING_PLAN.md`](./YMOS_PORTING_PLAN.md) 为准。

## 1. 状态图例

- ✅ 已落地：当前代码有可达入口和确定性验证。
- 🔄 部分落地：只吸收符合边界的机制，源项目其余形态仍排除。
- ♻️ 已覆盖：本项目已有等价或更严格实现，不重复复制。
- ⏸ 暂缓：有候选价值，但当前缺少可靠数据、用户入口或清晰产品收益。
- ❌ 排除：违反本地 DuckDB、只读数据、人工决策或安全边界。

## 2. 上游项目总览

| 来源 | 当前角色 | 已吸收能力 | 继续排除 / 暂缓 |
|---|---|---|---|
| `origin/main`（同仓上游） | 产品与正确性增量来源 | regime 接线、指数监控、回测与 UI、安全修复等按提交逐项适配 | TickFlow SDK、旧 HTTP/PG 数据链、整分支合并 |
| `../PA_Agent` | AI 工程机制来源 | 结构化输出运行时、校验/修复、attempt/cancel/usage、K 线分析上下文、profile 健康与显式 fallback、两阶段计划检查、artifact/通知、条件式连续性 | PyQt、秒级公网多源链、二元交易决定、自动交易、桌面专用连接器；M21 查询级复权暂缓 |
| `../YMOS` + `../ymos-diagnosis` | 交易纪律与策略治理语言来源 | append-only 生命周期、门禁、计划偏差、账户/NAV、红旗、盘后归因、策略 profile/坐标卡/playbook/提案审批 | 自动下单、以盈亏替代纪律、前端关闭结构红线、第二套领域词汇 |
| `../Vibe-Trading` | 研究、回测和导入健壮性来源 | Trade Journal 解析健壮性；组合风险思路按当前 canonical 日 K 重写为只读风险透视 | 外部 loader、券商实盘、LangGraph/Shadow Account、期权/加密/跨市场引擎 |
| `../daily_stock_analysis` | 可靠性和展示机制来源 | 选股诊断、超时守卫、Webhook 清洗、受控 fallback 的 single-flight/cache/backoff/circuit 模式 | 多公网 fetcher 主链、富途券商、Codex App Server、独立 Web/Electron 外壳 |
| `../fquant` | 数据口径与复盘语义参考 | three-locks、复盘分区语义；当前只读 DuckDB provider 所需口径 | 跨仓 `.env` 密钥耦合、HTTP provider、PostgreSQL 依赖 |
| `../go-stock` | 局部选股语义参考 | 本地可执行的选股预设语句 | 东财在线行情/选股 API；财经日历当前暂缓 |
| `../fstore` | DuckDB schema / generation 生产契约 | tickflow 只读消费 fstore generation | 迁移数据库服务、写入上游数据库 |
| `../engine` + `../duckdbsnap` | engine-a、catalog、manifest/current.json 生产契约 | staged catalog、immutable generation、freshness/pinning 校验 | 业务层直连 writer raw、catalog 失败回退 raw |
| `../fhold` | 真实账户/持仓及 Trade Journal 成交事实来源 | 持仓只读快照；通过 `fhold-cli tx snapshot --format json`（仅本地模式）读取一致快照并确认追加成交流水到 journal；无法证明一致性时 fail-closed | 直读 `~/.fhold/fhold.db`、券商写入或下单、把 fhold 成交写入 trading 生命周期事件流 |
| `../tdx-api` | 历史数据旁路 | 无运行时移植 | 永久不恢复为行情主链或 fallback |

## 3. 当前已落地能力账本

### 3.1 PA_Agent 工程机制

| 能力 | 状态 | 当前落点 |
|---|---|---|
| 日志与错误脱敏 | ✅ | `backend/app/log_redaction.py`、统一 `AppError` 语义 |
| 结构化 AI 解析、schema/不变量校验、分类限次重试 | ✅ | `backend/app/services/ai_structured/` |
| attempt/request、取消、进度、watchdog 与 usage | ✅ | `ai_attempts.py`、`agent_runner.py`、各 AI 入口 |
| K 线分析上下文、形成中 bar 排除、preflight、Prompt 预算 | ✅ | `analysis_context.py`、`stock_analyzer.py` |
| profile 健康、受控 fallback、模型/usage 展示 | ✅ | `ai_routing.py`、`ai_provider.py`、设置页 |
| 单 profile 主动健康探测 | ✅ | `POST /api/settings/ai/profiles/{id}/test`；强制不走 fallback，返回模型、耗时、usage 与脱敏错误 |
| 两阶段计划检查 | ✅ 默认关闭 | 只读已保存计划；Stage1 → 程序门禁 → 条件式 Stage2；不写交易事件 |
| append-only artifact、失败队列、导出、通知 | ✅ | `analysis_artifacts.py`、飞书/PushPlus 受控通知 |
| M25 连续性 | ✅ 条件式 | 只在计划检查中显式 opt-in；失配强制全量；新 artifact 保留 parent chain |

本轮额外加固了两个边界：Agent 流若未收到 `done/error` 终态，必须记为错误；分析 bar 和 JSON 输出中的无效日期、`NaN/±Inf` 不得冒充当前数据或泄漏到标准 JSON。

### 3.2 YMOS 交易纪律域

交易生命周期现在区分：

- `计划中`：已建档/准备，尚无真实成交；
- `建仓中`：已有部分成交，计划仍未显式收口；
- `持仓中`：`complete=true` 或 `finalizeOnly=true` 后进入持仓管理；
- `已平仓`：全部平仓并按 `tradeId` 幂等结转账户；
- `已作废`：零成交计划显式 `void` 的终态。

`add/trim` 在建仓阶段只调整计划总额，不伪造成仓位变化；实际仓位只能由 `fill/tp/sl/close` 改变。`trade_events.jsonl` 与 `decision_audit.jsonl` 继续只追加，终态拒绝后续写入，旧 `open/prepare/revise/fill/add/tp/sl/adjust/close` 请求仍保留兼容语义。

### 3.3 组合风险透视

`GET /api/trading/portfolio/risk` 只读当前 `建仓中/持仓中` 的真实数量，并复用 canonical 日 K 计算：

- 组合年化波动；
- 静态权重历史最大回撤；
- 最大两两相关性；
- 有效持仓数与最大权重；
- 每标的年化波动和风险贡献；
- 相关性矩阵、共同样本数、`dataAsOf`、缺失标的与 warning。

前端 `/trading` 的“组合风险透视”只展示后端结果，不重算。无持仓返回 `no_positions`；共同样本不足返回 `insufficient_data + degraded=true`，不伪造风险值。

### 3.4 受控外部补缺

外部公共源只允许经 `services/external_fallback/`，并继续满足：默认关闭、`realtime/depth` 独立 scope、仅补真缺口、provenance 标记、限速/缓存/熔断、仅展示、不写 canonical/enriched/sealed，不进入选股、回测或监控评估。

## 4. 本轮候选裁决

| 候选 | 裁决 | 理由 |
|---|---|---|
| 沪深两市融资融券**市场总余额** | ⏸ 暂缓 | 当前已经有用户显式拉取的 `ext_margin_em` 个股两融预设；市场总余额尚无本地 sealed/canonical 契约，也没有消费该总量序列的确定性研究/风控入口。为一个展示数字增加远端旁路会重复数据面并扩大 freshness/provenance 责任。 |
| 财经日历 | ⏸ 暂缓 | 当前交易/研究主路径没有事件日历领域模型、时区/重要性/修订口径和可验证的本地来源。直接复制 go-stock 的远端日历只会新增第二条网络数据链，且尚不能进入 canonical 回测或计划检查。 |
| AI 后端健康测试 | ✅ 已落地 | 属于配置诊断而非交易建议；用户主动触发、目标 profile 精确、15 秒/8 token 上限、禁止 fallback、错误脱敏。 |

暂缓不是待办欠账。只有同时具备“可靠来源与 provenance、明确消费入口、缺失/stale 语义、确定性测试”时，才重新评估两融市场总余额或财经日历。

### 4.1 2026-08-17 增量源码审计

审计范围为 2026-08-10 之后的本地可见 Git 提交，以及与已登记领域有关的未提交工作树变更。`origin/main` 在此区间没有新提交；它与当前分支之间仍存在更早、已在来源清单中明确不作隐式合并的历史差异。

| 来源 | 新发现 | 裁决 |
|---|---|---|
| `../PA_Agent`、`../YMOS`、`../ymos-diagnosis`、`../go-stock`、`../daily_stock_analysis`、`../fstore`、`../fhold` | 无基线后的本地可见提交 | ♻️ 无新增能力，不重复迁移。 |
| `../engine` | `ce3d2a0`、`f073199`、`5fa903a` 修复 callauction/migrate/fstore writer 和日级板块资金流的生产者死锁 | ♻️ 影响上游发布可靠性，但不改变 tickflow 已消费的表、generation、catalog 或 manifest 契约；应作为 engine 运维升级项，不移植到业务层。 |
| `../fquant` | 未提交的 A 股逐笔按交易日解析 staged catalog 路由与连接缓存 | ♻️ 当前 `catalog_resolver.py` + `tdx_duckdb_client.py` 已按日期 pin staged trans generation，且上游改动尚未提交。 |
| `../Vibe-Trading` | 未提交的 TDX CSV loader、TDX-first fallback 和 AkShare 成分股读取 | ❌ 与本地 DuckDB 主链、sealed 数据边界及“不新增公网主数据源”约束冲突。 |
| `../duckdbsnap` | 未提交的 manifest `stage/coverage_date/quality/reconciled/artifacts` 元数据与发布锁增强 | ⏸ 暂不接入：当前已发布 manifest 仍是 legacy 结构；本地 `catalog_resolver.py` 已 fail-closed 校验对应 staged route 元数据。仅当上游提交、生产者实际发布版本化字段并给出端到端兼容性 fixture 后，再评估消费者完整性校验；不得在热查询路径全量 hash 大型 DuckDB 文件。 |

## 5. 永久边界

1. 业务数据读取继续经 `data_providers` 和 capability 门控；不得恢复 TickFlow SDK、tdx-api、PG 或自建 HTTP 行情主链。
2. A 股 minutes/trans 继续由 staged catalog 定位，错误 fail-closed，不回退 writer-owned raw。
3. 外部 fallback 不得写 canonical/enriched/sealed，也不得进入回测、选股、监控或计划检查。
4. AI 不生成订单、方向、数量、建议价格或执行动作；程序门禁只可保持或降级。
5. 交易事件、决策审计、analysis artifact 与账户 changes/settlements 保持 append-only；不得为了兼容 UI 整份覆盖。
6. `fhold` 只能只读 CLI；不得直读数据库或接券商写操作。
7. 新上游代码必须翻译为当前领域契约；不得留下兼容 shim、别名或第二套状态/错误语言。

## 6. 维护流程

每次上游审计或移植都按以下顺序更新：

1. 在来源清单记录仓库、审计点和可访问性；
2. 逐项给出“已落地 / 已覆盖 / 暂缓 / 排除”结论；
3. 只实现有明确用户路径、确定性契约且不触碰红线的最小切片；
4. 更新本总账及对应专题计划，列出代码落点和真实验证；
5. 若改变 provider、generation/catalog、fallback 或运维语义，再同步 `AGENTS.md` 与 `FQUANT_INTEGRATION_PROGRESS.md`；
6. 不修改 `data/`，不自动提交。

验证至少包括受影响的后端测试、前端 TypeScript/构建检查，以及用户可见 UI 的运行态 smoke。真实外部 AI、通知或行情调用必须由用户主动触发；测试机制本身不得自动产生外部调用。
