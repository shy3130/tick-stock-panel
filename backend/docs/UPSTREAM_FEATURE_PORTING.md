# 上游功能移植与维护总账

> 日期：2026-08-10；最近增量审计：2026-08-25
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

### 4.2 2026-08-21 增量源码审计

审计范围为 2026-08-17 之后各来源仓库的本地可见提交与未提交工作树变更，并对照实际挂载的 DuckDB 快照做运行态验证（`DATA_PROVIDER=fquant_local` 下 provider 端到端冒烟 16 项通过、2 项合理 skip）。

| 来源 | 新发现 | 裁决 |
|---|---|---|
| `../fquant` | 未提交：A 股逐笔 trans 改为 `engsnap.ResolvePublishedRouteAt` 按交易日解析 staged catalog（带 root env 覆盖）；其余为 web 库路径从 `/Volumes/WD1` 迁到 `/Volumes/WD1/duckdb` 的默认值重组 | ♻️ 已覆盖：tickflow `catalog_resolver.py` 先行实现同语义（按日期 pin staged final/preliminary generation、root env 覆盖、fail-closed），路径重组对 tickflow 默认路径无影响。 |
| `../fstore` | 未提交 writer 变更：day_kline/minute_kline 从 `fd_*` payload_json 长表分区改为直表列（`tdate/cjl/cje/zf/zdf/zde/hsl` + `asset_type BIGINT` + `source_table`）；`daily_markets` 追加 `z50/z52/z53/tags` 列。**实际挂载的 `fstore-klines.duckdb.day_klines` 已是新 schema 且数据到 2026-08-20** | ✅ 兼容实证：`mapping.py:klines_rows_to_daily`、`snapshot_resolver.py` 宽松解析（`.get()`），provider 冒烟全绿；新列对现有查询前向兼容，无需改动。 |
| `../duckdbsnap` | manifest `stage/coverage_date/quality/reconciled/artifacts` 增强仍未提交；但生产者已开始在 engine-a published manifest 的 entries 上发布 `quality: "verified"` 字段 | ⏸ 维持暂缓：代码未提交、无端到端兼容性 fixture；tickflow 解析对该字段前向兼容（宽松 `.get()`）。发布侧字段落地是重评信号之一，待上游提交后再评估消费者完整性校验。 |
| `../engine` | 新提交 `cf752fe` 为 pi-rewind 会话元数据（非产品代码）；`pkg/snapshot`（catalog_build/catalog_lookup/resolve/verify_manifest）约 800 行在途未提交变更 | ♻️ 无可移植对象：在途变更无稳定基线；当前发布 catalog generation（`20260821T021106`）已被冒烟正常消费，观察待提交。 |
| `../fhold` | `93a2e3a`（2026-08-17）：`tx snapshot` 只读一致性快照（`NewReadOnlySnapshot`，SQLite mode=ro、不 mkdir/不 migrate）；CLI 显式 `--mode local\|http`，mode 为空或 `local` 走本地，未知模式硬失败 | ✅ 兼容：tickflow `fhold_client.py` 调用 `tx snapshot` 不传 `--mode` → 默认 local 只读路径；失败仍 fail-soft（`available=False`），契约不变。 |
| `../Vibe-Trading` | 未提交：`agent/backtest/loaders/tdx_local_loader.py` 直读 `/Volumes/vol3/tdx` 通达信导出 CSV 目录 + registry/runner 接线 | ❌ 维持排除：绕过本地 DuckDB 主链与 sealed 数据边界，另建 CSV 文件数据面。 |
| `../PA_Agent`、`../YMOS`、`../ymos-diagnosis`、`../go-stock`、`../daily_stock_analysis` | 无新提交，工作树干净（仅 `.mini-wiki/` 类未跟踪目录） | ♻️ 无新增能力。 |
| `origin/main` | 2026-08-17 后无新提交 | ♻️ 无增量。 |

本轮唯一代码改动是 tickflow 自身脚本缺陷修复：`backend/scripts/test_fquant_provider.py` 的 `get_minute` 冒烟窗口此前未按 catalog 发布水位钳制（引擎层 `engine.py` 已有 `get_minute_coverage` 钳制先例），在数据发布滞后于自然日时误触 fail-closed。现按 `get_minute_coverage()` 的 `latest_date` 钳制窗口末端；水位不可知时保持原窗口，让 fail-closed 原样暴露。reviewer 补充修复（P2）：水位完全落后于查询窗口起点时钳制会产生倒置窗口，`get_minute` 退到单日旧日期路径读取窗口外数据并误判通过——此场景现显式 SKIP 并带水位/起点日期。正常路径（4800 行）与倒置路径（mock 水位早于起点 → SKIP）均已运行验证。


### 4.3 2026-08-25 增量源码审计（origin 已改名 tick-stock-panel）

上游 origin（`shy3130/tickflow-stock-panel`）已被 GitHub 侧改名为 `shy3130/tick-stock-panel`（对应上游提交 d0f91fae）；两个名字解析到同一仓库（node_id `R_kgDOS-CukQ`，旧名重定向），无需新增 remote，fetch origin 即覆盖。本次 `git fetch origin main` 将本地 `origin/main` 从 `c278dd3` 推进到 `196af2f`，区间 **83 个提交**（78 个非 merge），提交日期跨度 2026-08-02 ~ 2026-08-25。4.1/4.2 两轮「origin/main 无新提交」的记录自此过期；该结论为何未反映这批提交，本地证据无法判定，不做归因。

83 个提交中 67 个触及本地分支同样修改过的路径（26 个的全部文件与本地改动重叠），维持「不 cherry-pick、不整分支合并，一律能力级重写」的既有口径。下表保留审计时裁决；审计完成后的首批落地见 4.4：

| 候选 | 裁决 | 关键证据 / 约束 |
|---|---|---|
| 盘后任务协作式取消 + 进度停滞判定 + 执行槽所有权（a6a4bcd） | P0 移植候选 | 本仓 `pipeline_jobs.py` 取消后 `progress()` 静默返回、工作线程无取消信号（运行时复现）；须区分 cancelled/failed 终态，僵尸线程不得释放新任务的执行槽，慢任务按进度心跳而非总时长判活 |
| 自选 M:N 分组 + `scope=watchlist_group` 动态监控（2a2d3b7/83b96e2/254e1962） | P0 移植候选 | 本仓 watchlist 无分组模型；分组成员变化下一轮评估自动生效；分组删除/空组必须 fail-closed，禁止退化为全市场 |
| 多日分时（2a2d3b7） | P1 移植候选 | provider 跨日/跨 catalog route 合并已具备（`test_provider_minute_freq` 全模块 4 passed），缺 minute-range 聚合 API 与前端；历史日期禁 chart_live |
| AI 生成自定义信号（8519a2b） | P1 移植候选 | 复用本仓 `run_structured_ai` + `custom_signals` 白名单校验，不搬上游自研 JSON 修复器；AI 只产条件草稿，用户显式确认后走既有保存 API |
| 情绪周期 phase 体系 + 主线识别（697c27b/7c30f5b/6f0786a/65f46a1） | P1 移植候选 | 数据面须改读本仓 canonical/enriched；概念成分为当前快照非 PIT 必须显式告警；不引入仓位/操作提示；与既有 5 档 state 并存不替换消费方 |
| 交易所口径异动监控（68ce2337/21260f1/0b57c33） | P1 移植候选 | 基准动量只允许本地 canonical 指数 + 本地 realtime；外部 fallback 数据不得进入监控判定；建议与自选分组落地后接 `watchlist_group` scope |
| 因子/策略挖掘（697c27b/a0cb8991） | P2 移植候选（独立立项） | 只吸收嵌套样本外验证/因子相关去重算法并接入本仓 run_store/DurableJob/metrics 契约；不引入第二套 store、job 与指标口径 |
| 数据源插件化 UI、fuyao 同花顺插件（ea746b9） | ❌ 排除 | 需密钥的外部实时链与本地只读主链、受控 fallback 边界冲突 |
| 停机缺口 raw/enriched 删写自愈（e15acdd）、僵死 publishing 标记恢复（ff4274a） | ❌ 排除 | 前者建立在 raw mirror 可写假设上（本仓 stock raw mirror 禁写）；后者针对上游 EnrichedPublication 模型（本仓无此模型） |
| TickFlow 档位卡片 / Codex CLI / 品牌与截图类更新 | ❌ 排除 | 与本仓 provider 中立、AI profile 路由与 FM 品牌演进冲突 |

### 4.4 2026-08-25 首批能力级落地

本轮按 P0 → P1 顺序完成四个切片，均按当前领域契约重写，没有 cherry-pick 上游提交，也没有恢复 TickFlow SDK、外部行情主链或 stock raw mirror 写入。

| 能力 | 状态 | 当前落点 / 边界 |
|---|---|---|
| 盘后任务协作取消、进度停滞与全局执行槽 | ✅ 已落地 | `pipeline_jobs.py` 统一终态与执行权；取消信号进入工作线程，僵尸任务不能释放新任务槽；API/页面统一展示 `cancelled`。 |
| 自选 M:N 分组与分组监控 | ✅ 已落地 | 分组元数据独立存储，单标的可属于多组；旧 schema 原子备份后迁移；`scope=watchlist_group` 每轮动态解析，分组缺失、空组或损坏时 fail-closed。 |
| AI 生成自定义信号草稿 | ✅ 已落地 | `run_structured_ai` 只返回字面量草稿；字段、运算符、条件数、有限数和 ID 均经本地校验；不写盘、不执行，用户必须在既有编辑器显式保存。 |
| 交易所口径异动监控 | ✅ 已落地 | 3/10/30 个市场交易日逐日偏离和；本地 canonical 指数与本地 realtime 修正；ST 仅标记/展示过滤；异常规则独立约 30 秒边缘触发，可绑定全市场、指定标的或自选分组；基准缺失不以 0 代替。 |

验证覆盖本轮 178 项聚焦后端回归、3 项规则类型切换前端回归、前端 TypeScript + Vite production build，以及自选分组、AI 草稿、异动监测、规则方向归一和交易路由的运行态 UI smoke。未进入本轮的多日分时、情绪周期/主线识别与因子挖掘仍保持 4.3 的候选状态。

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
