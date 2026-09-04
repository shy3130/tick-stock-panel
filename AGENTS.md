# AGENTS.md — tickflow-stock-panel 项目身份卡

> **给接手这个项目的 AI Agent / 开发者看的"我是谁、我在干什么、怎么动我"速查卡。**
> 这不是设计文档，也不是入门教程——这是**写在仓库根的长期上下文**，让任何后续 Agent 在 5 分钟内搞清项目骨架与红线。
> 权威进度见 `backend/docs/FQUANT_INTEGRATION_PROGRESS.md`。

---

## 1. 项目定位

**tickflow-stock-panel** 是一个 A 股**选股 + 监控 + 回测**一体的工作台（前后端同仓），通过 **`data_providers` 抽象层**对接数据源。

- **前端**：React 18 + Vite + TypeScript + Tailwind + Tanstack Query + Lightweight Charts + ECharts（`frontend/`）
- **后端**：FastAPI + Pydantic v2 + APScheduler + Polars（计算） + DuckDB（查询） + Parquet（存储）（`backend/`）
- **回测**：vectorbt（项目内唯一的 pandas 边界）
- **AI**：可选 OpenAI 兼容接口（DeepSeek / 通义 / Ollama 等），用于生成策略与个股四维分析；自由 Agent 可在源码开发环境显式切到 Pi Agent Harness sidecar 试点，默认仍为 Python runtime

**核心架构演进**：原本只接 TickFlow SDK（付费）；从 2026-07-08 起，`FQuantProvider v2` 已收敛为只读本地 DuckDB（`fstore*.duckdb` + `tdx*.duckdb`，含港股拆分库），业务层仍通过 `data_providers` 抽象层切换 provider 名称。

**不是**对标同花顺 / 通达信的全功能客户端，**不**内置 AI 荐股 / 涨停预测。

---

## 2. 数据源矩阵

通过 `DATA_PROVIDER` 环境变量或 `/api/settings/preferences/data-provider` 在两个 provider 之间切换；环境变量优先级最高。

| Provider | 数据来源 | capabilities | 默认 | 切换方式 |
|----------|---------|--------------|------|----------|
| `fquant_local` | A 股点查/窄区间行情（wide/xdxr/minutes/trans/moneyflow 点查）走 engine dataquery v2 HTTP（`FQUANT_DATAQUERY_ENABLED`，默认开；0=整链回退 legacy DuckDB）；其余全部本地 DuckDB（默认 raw 路径，经 `snapshot_or_raw` 解析为 `snapshots/<root>/<gen>/` 只读 generation 快照；快照未发布时回退 raw 只读）：`fstore.duckdb` / `fstore-markets.duckdb` / `fstore-klines.duckdb` / `fstore-minutes.duckdb` + `tdx.duckdb` / `tdx-hk.duckdb` / `tdx-hkminutes.duckdb` / `tdx-hktrans.duckdb` | 日 K / 分钟 / 复权 / 财务 / realtime 快照 / universes；扩展逐笔/日级资金流；港股 K/minutes/trans；**stock raw mirror 禁写**；**depth 缺口**；**批量/长扫描在 v2 路径显式 blocked（等 engine #9/#11）** | ✅ 默认 | `DATA_PROVIDER=fquant_local` 或 settings API |
| `fquant` | 同一 DuckDB 实现，保留 provider 名称兼容 | 同上；**depth 缺口** | ❌ | `DATA_PROVIDER=fquant` 或 settings API |

**fquant 本地源**：

| 上游 | 协议 | 用途 | 默认地址 |
|------|------|------|---------|
| fstore DuckDB | DuckDB read-only | 标的列表 / 财务报表 / 复权事件 / universes / 小表 | `FQUANT_FSTORE_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/fstore.duckdb`，解析为 `snapshots/fstore/<gen>/` 快照） |
| fstore markets DuckDB | DuckDB read-only | realtime 快照 / 每日行情 | `FQUANT_FSTORE_MARKETS_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/fstore-markets.duckdb`，解析为 generation 快照） |
| fstore klines DuckDB | DuckDB read-only | fstore K 线兼容表 | `FQUANT_FSTORE_KLINES_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/fstore-klines.duckdb`，解析为 generation 快照） |
| fstore extended DuckDB | DuckDB read-only | 财务三表 / 复权事件 | `FQUANT_FSTORE_EXTENDED_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/fstore-extended.duckdb`，解析为独立 `snapshots/fstore-extended/<gen>/` 快照） |
| TDX DuckDB | DuckDB read-only | 日 K wide/day / xdxr / 日级资金流 | `FQUANT_TDX_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/tdx.duckdb`） |
| engine dataquery v2 | HTTP（httpx，无重试，typed error） | A 股 stock 点查/窄区间：series `day\|wide\|minutes\|trans\|xdxr`（cache_id `^(sh\|sz\|bj)\d{6}$`）+ moneyflow daily/minute 点查（≤16 标的）；版本元数据 fail-closed；批量/区间超界一律 `DataQueryBlockedError` | `FQUANT_DATAQUERY_BASE_URL`（默认 `http://127.0.0.1:8099`）+ `FQUANT_DATAQUERY_TIMEOUT_S`（默认 5）+ `FQUANT_DATAQUERY_ENABLED`（默认 1） |
| TDX A 股 minutes 路由 | 发布 catalog + DuckDB read-only | 按交易日定位 2023 年前归档或当前 minutes 快照（staged，preliminary→final） | `FQUANT_SNAPSHOT_ROOT_CATALOG` + `FQUANT_SNAPSHOT_ROOT_ENGINE_A{,_PRELIMINARY,_MINUTES_ARCHIVE}` |
| TDX A 股 trans 路由 | 发布 catalog + DuckDB read-only | 按交易日定位历史归档年片或活跃年的月度 trans 快照（staged，preliminary→final） | `FQUANT_SNAPSHOT_ROOT_CATALOG` + `FQUANT_SNAPSHOT_ROOT_ENGINE_A{,_PRELIMINARY,_TRANS_ARCHIVE}` |
| ordered-trans 研究 generation | published immutable Parquet read-only | raw trans 离线保序 materialize 为 sparse true-trade 1m；runtime 仅经 provider factory 读取，强制 48×5m/16×15m 窗口 | `FQUANT_SNAPSHOT_ROOT_ENGINE_A_ORDERED_TRANS`（默认 `/Volumes/WD1/duckdb/snapshots/engine-a-ordered-trans`） |
| TDX HK DuckDB | DuckDB read-only | 港股日 K / 多周期 K | `FQUANT_TDX_HK_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/tdx-hk.duckdb`，解析为 engine-hk generation 快照） |
| TDX HK minutes DuckDB | DuckDB read-only | 港股分钟 K | `FQUANT_TDX_HK_MINUTES_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/tdx-hkminutes.duckdb`，解析为 engine-hk generation 快照） |
| TDX HK trans DuckDB | DuckDB read-only | 港股逐笔成交 | `FQUANT_TDX_HK_TRANS_DUCKDB_PATH`（默认 `/Volumes/WD1/duckdb/tdx-hktrans.duckdb`，解析为 engine-hk generation 快照） |

**已知缺口**：

- **depth（5 档盘口）当前缺口**：FQuantProvider 目前不暴露 depth capability，`depth_service.py` 已做能力门控降级；可通过「受控外部 fallback」（默认关闭，见第 4 节契约）补公共免费源五档，未开启时维持降级返回空
- **realtime 已接入**：只读本地 `fstore-markets.duckdb.daily_markets` 的 generation 快照（最新）；先取全局 `MAX(trade_date)`，再按该交易日与 `asset_type` 点查；使用独立 DuckDB 客户端/连接锁，避免被财务或 K 线查询阻塞；不再调用 `tdx-api` / sina / tencent / `../fquant` HTTP
- **universes 已接入**：阶段 3.2 走 provider `get_by_universes()`；fquant 接 fstore `chengfen_gu` + `base_infos`
- **dataquery v2 cutover（2026-09-03，Issue #56）**：A 股点查/窄区间走 v2 HTTP；v2 rows 紧凑 `YYYYMMDD` 日期在 `_query_series` 出口统一归一 ISO；moneyflow daily 只有 total 四字段（`main_*` 显式 None 不冒充）；全市场批量（symbols>16、区间>2500 行、minutes>31 日、moneyflow range）一律 `DataQueryBlockedError` 等 engine #9/#11 pinned bundle；chips / HK / ETF / index / call auction / fstore 域不迁移
- **ordered-trans 研究链已接入**：`ordered_trans_research` capability 只打开独立 published generation；runtime 不读 raw CSV。artifact 不回填收盘集合竞价零成交分钟，按 sparse true-trade 1m 的 timestamp bucket 验证 48×5m；首个 bounded generation 仅覆盖 `600519.SH/000001.SZ/300750.SZ` 30 个完整日，真实因子 verdict 为 `rejected`，不进入短线池/Agent/默认策略

---

## 3. 关键文件索引（必读）

### 数据源层（`backend/app/data_providers/`）

| 文件 | 行数 | 作用 | 必读理由 |
|------|------|------|---------|
| `base.py` | 70+ | `MarketDataProvider` 协议 + `ProviderCapabilities` | **接口契约**，新增 capability 必须先改这里 |
| `fquant_provider.py` | 600+ | FQuantProvider（v2，本地 DuckDB 聚合） | 直连 fstore DuckDB / TDX DuckDB |
| `fquant/` | 10+ 文件 | fquant 子模块（symbols / fstore_duckdb_client / engine_data_duckdb_client / mapping / adj_factor / raw_reconstruct / fallback） | 改 fquant 行为时从这里入手 |
| `fquant/daily_market_research.py` | — | 固定 published markets generation，按 symbol/date 读取历史名称、ST/板块制度与 exact `ztj` | 只提供 PIT 事实；缺字段删失，禁止由当前名称或 K 线反推 |
| `normalizer.py` | — | 字段规范化（Symbol / Instrument / KLine / Realtime 等） | 既有契约稳定；realtime 契约为追加 |
| `registry.py` | 20+ | provider 注册中心（`get_provider(name)`） | 新增 provider 只需在这里 +1 行 |
| `schemas.py` | — | Pydantic schema | **未修改** |

### Service 层（已解耦 7/7）

| 文件 | 改动量 | 角色 |
|------|--------|------|
| `services/kline_sync.py` | +105 / -92 | **解耦试点**，其他 service 照抄它的 `_get_data_provider()` 模式 |
| `services/instrument_sync.py` | +35 / -40 | 标准解耦 |
| `services/quote_service.py` | +46 / -17 | realtime 走 provider；状态 `ready` 必须同时满足最近轮询成功、非空、在 freshness 窗口内且源日期为当日；fquant 走 `fstore-markets.duckdb.daily_markets` generation 快照 |
| `services/financial_sync.py` | +87 / -34 | 财务报表走 fstore |
| `services/index_sync.py` | +28 / -31 | universes 走 provider，FQuant 走 fstore |
| `services/watchlist.py` | +20 / -5 | realtime 走 provider；fquant 走本地源 fallback |
| `services/depth_service.py` | +20 / -0 | 能力检查模式：fquant 直接降级返回空 |
| `services/n_shape_research_data.py` | — | N 字研究 composite reader：绑定 canonical raw OHLCV 与 markets PIT facts 两份 generation/manifest；任一来源缺失即 unavailable，构造后不跟随 `current` |

### 研究控制面（`backend/app/research/` + `frontend/src/features/research/`）

|文件|作用|红线|
|---|---|---|
|`app/research/catalog.py` / `contracts.py`|19 项公开 factor 的唯一注册表、Pydantic 参数 schema、scope、数据依赖与五类结果 profile|工程/数据/verdict/promotion 四套状态独立；不得新增第二注册表或因子专用协议|
|`app/research/preflight.py` / `control.py` / `adapters.py`|统一预检、创建入口与既有 evaluator 适配|运行前冻结 request；领域 unavailable 正常收口，程序错误不得伪装 unavailable；不得改变 evaluator|
|`app/research/job_store.py` / `run_store.py` / `runner.py`|持久化 Job、不可变 Run artifact、interactive worker 与 SSE|summary JSON + events/series Parquet；取消/恢复必须终态落盘；前端不得重算指标|
|`app/research/worker.py` / `services/full_market_adapters/`|11 项全市场研究的单实例受控子进程|API 只传白名单 run_id；完整参数经统一 registry 验证；资源超限和锁冲突必须落明确终态|
|`app/api/research_runs.py`|factor catalog/detail、preflight、runs/events/series/SSE、证据关联统一接口|旧 19 个 factor POST 与 capability GET 已删除，不得恢复|
|`features/research/`|Overview、Catalog、Workbench、Run Center/Detail、Evidence、Data、Automation、Analytics|七类受控参数控件；loading/empty/error/unavailable 分离；旧 `pages/Research.tsx` 和旧 research client 已删除|
|`docs/RESEARCH_WORKBENCH_V2_DESIGN.md`|Research Workbench V2 权威契约与完成定义|Run 不自动进入策略池、Agent 候选或交易执行；factor Run 不冒充 recap run-card|

### 回测域（`backend/app/backtest/` + `backend/app/api/backtest*.py`）

| 文件 | 作用 | 红线 |
|------|------|------|
| `app/backtest/run_store.py` | **BacktestRun 唯一持久化契约**（`data/research/backtest_runs/{run_id}.json`，不可变事实，仅 `favorite`/`label` 可 PATCH）；列表/比较/导出/旧 run_card 只读惰性迁移 | 旧 `research/run_cards/*.json` 对回测域只读（DELETE 403、PATCH 先固化再改）；20 MiB 上限、原子写、run_id 白名单；`save_run_card` 仅剩 AI 池研究与定时研究两个非回测域调用方 |
| `app/backtest/job_store.py` / `job_recovery.py` | SSE 任务 DurableJob（`data/research/backtest_jobs/`）+ 启动时回收外来 lease 的 running/pending 为 `interrupted` | 取消必须落盘 `cancelled`，禁止当 interrupted 续跑；策略/因子无引擎 checkpoint，重连同一 query 只整单重跑；lifespan 只标记不自动开跑 |
| `app/backtest/metrics.py` | `MetricContext` 统一年化口径（频率唯一输入、`risk_free_rate` 显式、`ddof=1`）+ 全套绩效/风险/相对指标与 Bootstrap | 频率与年化系数冲突必须拒绝；`payoff_ratio` 与 `profit_factor` 是两个独立契约，不得混用 |
| `app/backtest/provenance.py` | 数据快照元数据（canonical/adjustment generation、股票池定义、`snapshot_hash`）、engine/metric 版本 | 全市场股票池无法证明 point-in-time 时必须保留 `survivorship_bias` 告警 |
| `app/backtest/engine.py` / `strategy.py` / `factor.py` / `robustness.py` / `optimizer.py` | 主 Polars/NumPy 撮合与策略/因子/稳健性/寻优服务（T+1、涨跌停、整手、费用滑点、持仓期 MAE/MFE、参数扰动、严格 Walk-Forward、训练/留出笛卡尔搜索） | 旧 vectorbt 入口 `POST /api/backtest/run` 仅 legacy（固定 `legacy_vectorbt_engine` 告警），停止新增消费者；寻优不得宣称全局最优，不得自动写入策略池 |
| `app/backtest/attribution_report.py` | 交易窗口 Brinson-Fachler 行业归因（当前行业映射、相对等权已执行交易样本） | 映射非 point-in-time；输入/行业不足必须 fail-closed；无冻结可审计本地因子序列时 Fama-French 必须显式 unavailable，禁止代理结果 |
| `app/backtest/universe_gating.py` / `style_factors.py` / `regime_breakdown.py` / `cost_sensitivity.py` / `fill_reachability.py` | V4 可信度增强：上市天数门控（provider `get_stock_reference_flags` 的 ssdate，删行实现 + 统计）、本地 SMB/UMD/LMV 三因子构建与 OLS 归因、市场状态四桶、成本倍数敏感性、分钟级成交可达性抽查 | 无 HML（无账面市值历史，不伪造代理）；regime 波动阈值为事后全样本口径（仅分组解释）；fill-reachability 是诊断不是撮合能力；上市日期不可用时 fail-open 但必须显式计数/告警 |
| `app/backtest/portfolio_combine.py`、`app/jobs/backtest_favorite_rerun.py` | 路线图三期/四期：组合净值合成（账户追踪法，daily/monthly/none 再平衡）、盘后定时复跑收藏策略（偏好 `backtest_auto_rerun` 默认关，滚动窗，label=定时复跑） | 组合合成是事后加权非共享资金池撮合，不落盘不生成新 Run；定时复跑单失败不阻塞，开关关闭零开销 |
| engine.py 分钟撮合路径（`build_minute_execution` / `MinuteExecutionData`） | `bar_precision='minute'`：VWAP 窗口（09:30-09:45 / 14:45-15:00）+ 风控盘中触发（触线按线价、跳空取分钟 open、双触取不利方向） | 仅 position 模式；标的 ≤100 且区间 ≤120 交易日；缺数据回退日 K 必须计 `minute_fallback_daily`；strategy_cancel 的 job_key 重建必须关键字传参（位置传参已出过一次错位 bug） |
| `app/api/backtest.py` | 策略/因子/组合回测 + `/runs` 列表/读取/比较/复跑/导出/PATCH/DELETE | Run 落盘失败必须在响应带 `persisted=false` 与 `persistence_failed` 告警，不得伪装成功 |
| `app/api/backtest_optimizer.py` | `GET /universes` + `POST/GET/SSE/cancel/resume` 策略寻优实验 | 训练窗打分、留出窗确认；DSR/PBO 是诊断不是准入；全市场/板块/行业池必须带幸存者偏差告警；resume 必须用磁盘冻结窗口，禁止 `resolve_window`；缺训练曲线时 DSR/PBO 置空并标 `resumed_partial_diagnostics` |
| `frontend/src/pages/backtest/` | 运行历史（RunHistoryPanel）、专业诊断、稳健性、参数网格（含回填策略表单）、策略寻优、交易明细筛选、行业归因与独立 HTML 报告下载 | 前端不得重算风险指标，非有限数值显示"—"；未持久化 Run 不得提供报告下载 |

权威口径与能力边界见 `backend/docs/BACKTEST_MATURITY_IMPROVEMENT_PLAN.md`（§5.4 工程决策、§12 未实现清单——现有 `walk_forward` 为严格 IS/OOS：训练选参→冻结参数→独立 OOS；候选仅局部单参数邻域。策略寻优 V1 是另一条独立搜索：策略×股票池×持仓周期笛卡尔展开 + 冻结训练/留出，不是 Optuna/全局参数优化）。
设计见 `backend/docs/STRATEGY_SEARCH_DESIGN.md`。

### AI Agent 运行时试点

| 文件 | 作用 | 红线 |
|------|------|------|
| `services/agent_runtime.py` | `python` / `pi` 运行时 seam；Pi 子进程 NDJSON 协议、工具回调、取消和清理 | 只允许 `openai_compat` profile；每次 attempt 固定 runtime，禁止静默 fallback |
| `services/agent_runner.py` | session/bus/attempt 生命周期；按 `AGENT_RUNTIME` 选择 runtime | 既有 SSE `delta/tool_call/tool_result/done/error` 契约不得改变 |
| `services/short_pool.py` / `services/agent_research_tools.py` | `screen_stock_pool` 的固定 `short_momentum_quality_v1` 分支：canonical QueryService 筛选、逐股证据、request-local 内容寻址 `pool_id`、不写 user_data artifact | 仍是现有 13 工具之一；条件/排序只在服务端定义，Agent 只能传 `limit=5..12`，不得增删重排候选；该 `pool_id` 不得传给旧 `start_pool_backtest` |
| `pi-agent-worker/` | 独立 Node ≥22.19 sidecar，使用 `@earendil-works/pi-agent-core` + `@earendil-works/pi-ai` | 模型侧只注册 Python 桥接工具；sidecar 源码不得直接执行业务 I/O；source/dev 试点不宣称具备 OS sandbox |
| `docs/PI_AGENT_PILOT_PLAN.md` | 试点边界、风险、验收矩阵和退出标准 | 当前仅 source/dev；Docker/PyInstaller 暂不接入 |

### Trading 纪律域（`backend/app/services/trading/`）

| 文件 | 作用 | 红线 |
|------|------|------|
| `lifecycle.py` / `store.py` | 单笔状态机（计划中/建仓中/持仓中/已平仓/已作废）+ `trade_events.jsonl` / `decision_audit.jsonl` append-only 事实流；分批 fill、计划 add/trim、零成交 void | 历史事件和审计只能追加，禁止整份覆盖；`add/trim` 仅改变计划、不改变真实仓位，`add` 可从持仓中重开建仓，终态拒绝后续事件 |
| `accounts.py` / `portfolio.py` | 账户资金基数、幂等平仓结转、NAV/敞口/健康度快照；canonical 日 K 驱动的只读组合风险透视 | 行情估值仍必须走 `data_providers`；前端不得重算风险，非有限数值必须以 `null` 输出 |
| `fhold_client.py` | 只读调用 `fhold-cli --format json` 获取 `../fhold` 真实券商账户/持仓，以及供 Trade Journal 一致快照预览/追加的成交流水 | 仅只读事实，不是行情 provider；journal 导入只能通过 `tx snapshot`（仅本地模式）preview-then-apply、按 fhold 原始交易 ID 去重，无法证明一致性时 fail-closed，绝不写回 fhold 或 `trade_events`；禁止绕过 CLI 直读 `~/.fhold/fhold.db`；不可用时 fail-soft |
| `gates.py` / `plans.py` | 五条后端结构红线、用户清单、盘前计划与计划/实际偏差 | 结构红线不可由前端或用户配置关闭 |
| `plan_check.py` | 默认关闭的两阶段计划检查：Stage1 canonical K 线诊断 → 程序门禁 → Stage2 用户计划审查；输出 append-only artifact 与 trace | 只读已保存计划；程序门禁只可保持或降级；AI 不得输出订单/方向/建议价格/执行动作；不得写 `trade_events` 或进入 screener/backtest/monitor |
| `red_flags.py` / `red_flag_webhook.py` | 放宽止损、亏损加仓、绕门、审计断链、期限超限、仓位超限、门禁膨胀（global 分组）检测；可选去重 Webhook | 红旗与盈亏无关；推送失败不得阻断事实落盘 |
| `review_job.py` | L0/L1/L2 状态驱动盘后归因（L0 零 AI 调用；L1 按事件数去重） | AI 未配置走 `blocked_by_dependency`，不得中断调度 |
| `proposals.py` / `autopsy.py` | AI 四分类归因（12 不一致模式 rubric）、带反证条件的策略变更提案与人工审批状态机；疑似亏损后放宽自动打 `relaxationAfterLoss` | 单笔结果不自动改策略；AI 不能替代人工批准 |
| `services/strategy_profile.py` / `strategy_validator.py` | 策略失效信号、风险/期限声明、family 坐标卡与 playbook、7 项机械体检 | 失效信号必须 `name/observable/action` 三要素齐全；family=mixed 必须声明裁判归属四要素 |

### 文档（团队权威）

| 文件 | 作用 |
|------|------|
| **`backend/docs/FQUANT_INTEGRATION_PROGRESS.md`** | **进度文档（权威）**——阶段划分、决策记录、风险、变更日志，每次 commit 前校对 |
| `backend/docs/FQUANT_PROVIDER_DESIGN.md` | 846 行设计稿（三源实测 + 架构） |
| `backend/docs/FQUANT_PROVIDER.md` | 旧 PoC 说明（已被 v2 覆盖，仅供回溯） |
| `backend/docs/YMOS_PORTING_PLAN.md` | YMOS 纪律层移植设计、契约与完成进度 |
| `backend/docs/BACKTEST_MATURITY_IMPROVEMENT_PLAN.md` | 回测专业化审计与改进计划——P0 口径修复、BacktestRun 契约、工程决策与未实现边界（权威） |
| `backend/docs/BACKTEST_PRODUCT_REVIEW_2026-08-20.md` | 回测模块产品评审与路线图（2026-08-20）——能力盘点、易用性/专业性缺口、P0-P2 功能清单与分期 |
| `backend/docs/SCREENER_PRODUCT_REVIEW_2026-08-20.md` | 股票筛选模块产品评审与路线图（2026-08-20）——策略选股/条件选股双入口盘点、双宇宙与名实不符等 P0、P0-P2 功能清单与分期 |
| `backend/docs/AI_PRODUCT_REVIEW_2026-08-21.md` | AI 模块产品评审与路线图（2026-08-21）——Report/Structured/Agent 三类运行时盘点、荐股红线、P0-P2 功能清单与分期 |
| `backend/docs/PA_AGENT_PORTING_PLAN.md` | PA_Agent 工程机制移植总账、决策门、已交付边界与明确暂缓项 |
| `backend/docs/UPSTREAM_FEATURE_PORTING.md` | 上游项目、已移植能力、暂缓/排除项与维护流程总账 |
| `backend/docs/PI_AGENT_PILOT_PLAN.md` | Pi Agent Harness 可选 sidecar 试点的架构、风险、验收与退出标准 |
| `README.md` | 用户向快速开始；末尾有"本地开发与数据源"开发者附录 |

### 测试

| 文件 | 作用 |
|------|------|
| `backend/scripts/test_fquant_provider.py` | 16 项端到端冒烟，真实源不可达项单独列 skip |
| `backend/scripts/test_trading_lifecycle.py` | Trading 全链路隔离数据 E2E 冒烟（不修改 `data/` 用户数据） |
| `backend/tests/backtest/` + `backend/tests/api/test_run_store_api.py` | 回测域单元/API 测试：指标口径（`test_metrics.py`）、Run 持久化与比较（`test_run_store.py`）、因子成本（`test_factor_costs.py`）、数据快照（`test_provenance.py`）、撮合/MAE-MFE（`test_strategy_backtest_correctness.py`、`test_trade_excursions.py`）、严格 Walk-Forward（`test_walk_forward.py`）、稳健性（`test_robustness.py`） |

---

## 4. 解耦约定（红线）

**所有 service 文件必须遵循**（参照 `kline_sync.py` 试点）：

```python
# ✅ 正确：用 _get_data_provider() 工厂
def _get_data_provider():
    global _provider_instance
    if _provider_instance is None:
        from app.data_providers.registry import get_active_provider_name, get_provider
        provider_name = get_active_provider_name()
        _provider_instance = get_provider(provider_name)
    return _provider_instance

def sync_daily(...):
    provider = _get_data_provider()
    df = provider.get_daily(symbols, start, end, asset_type)
    ...

# ❌ 错误：新增任何绕过 data_providers 的 SDK/HTTP/DB 直连
```

**进入业务入口前必须做 capabilities 检查**（fquant 的 depth 当前是缺口，realtime 依赖本地源可用性）：

```python
provider = _get_data_provider()
if not provider.capabilities.realtime:
    return []  # 或抛 FeatureUnavailable
result = provider.get_realtime(symbols)
```

**绝对不能**直接修改：

- `data_providers/base.py` 的接口契约（除非新增 capability 字段，并同步更新 `schemas.py` + 所有 provider）
- `data_providers/normalizer.py` 的字段语义
- `data_providers/registry.py` 已注册的 provider 名字（`fquant_local` / `fquant`）

**受控外部 fallback 契约**（2026-08-05 起生效；完整设计见 `backend/docs/CONTROLLED_EXTERNAL_FALLBACK_DESIGN.md`）：

外部公共免费行情不再一刀切禁止，但必须走**独立的 fallback 适配层**（`services/` 侧能力门控，不进 FQuantProvider），并同时满足：

1. **默认关闭**：`preferences.external_fallback_enabled` 默认 false，用户显式开启后对应能力才激活；
2. **仅补真缺口**：只覆盖本地源确实没有的能力（depth 五档、快照过期时的 realtime 快照级读取、当前交易日且本地目标日期为空的 `chart_live` 单标的图表展示）；本地 DuckDB 已有目标交易日数据时一律不走外部；
3. **provenance 全程标记**：返回行带 `source` 字段，API/SSE 响应带 `degraded` 标志，UI 有角标；
4. **绝不污染主链路**：不写 stock raw mirror、不写 enriched 分区、不进入回测 / 选股 / 监控评估输入（它们只读 sealed 分区）；
5. **口径校准 pinning 测试**：每个源必须有锁死单位 / 复权 / 时区 / 符号映射的回归测试（照 `fquant/mapping.py` 校准注释先例）；
6. **限速 + 熔断 + 缓存**：复用 `eastmoney_client` 模式（Host 白名单 + 最小间隔 + `trust_env=False`），连续失败自动熔断并通知。

**永久豁免（不适用 fallback）**：A 股历史 minutes/trans、以及任意 catalog 路由异常（仍 fail-closed 返回 503）；付费 / 需密钥源（TickFlow SaaS、Tushare Pro）；券商 SDK（Futu / Longbridge 等真实账户接口）。

**绝对不能**直接连接：

- 外部 Tencent / 新浪 / 第三方行情接口——除上述受控 fallback 适配层（含默认关闭、仅当前交易日单标的的 `chart_live`）外，FQuantProvider 保持只读本地 DuckDB，业务层不得自行直连
- 任何绕过 `data_providers` 抽象层（及受控 fallback 适配层）的 HTTP / DB 直连

---

## 5. 本地开发流程

### 环境变量

```bash
# 必填：provider 切换
export DATA_PROVIDER=fquant_local   # 或 fquant

# 可选：DuckDB 路径，不填则使用 /Volumes/WD1 默认挂载
export FQUANT_FSTORE_DUCKDB_PATH=/Volumes/WD1/duckdb/fstore.duckdb
export FQUANT_FSTORE_MARKETS_DUCKDB_PATH=/Volumes/WD1/duckdb/fstore-markets.duckdb
export FQUANT_FSTORE_KLINES_DUCKDB_PATH=/Volumes/WD1/duckdb/fstore-klines.duckdb
export FQUANT_FSTORE_EXTENDED_DUCKDB_PATH=/Volumes/WD1/duckdb/fstore-extended.duckdb
export FQUANT_TDX_DUCKDB_PATH=/Volumes/WD1/duckdb/tdx.duckdb
export FQUANT_TDX_HK_DUCKDB_PATH=/Volumes/WD1/duckdb/tdx-hk.duckdb
export FQUANT_TDX_HK_MINUTES_DUCKDB_PATH=/Volumes/WD1/duckdb/tdx-hkminutes.duckdb
export FQUANT_TDX_HK_TRANS_DUCKDB_PATH=/Volumes/WD1/duckdb/tdx-hktrans.duckdb

# A 股 minutes/trans 按日期从 engine 发布的 catalog 解析；所有 root 共享
# /Volumes/WD1/duckdb 默认挂载根，测试和 staging 可重定向各 root。
# 发布顺序与回滚见下方「catalog/engine 发布顺序」。
export FQUANT_SNAPSHOT_ROOT_CATALOG=/Volumes/WD1/duckdb/snapshots/catalog
export FQUANT_SNAPSHOT_ROOT_ENGINE_A=/Volumes/WD1/duckdb/snapshots/engine-a
export FQUANT_SNAPSHOT_ROOT_ENGINE_A_PRELIMINARY=/Volumes/WD1/duckdb/snapshots/engine-a-preliminary
export FQUANT_SNAPSHOT_ROOT_ENGINE_A_MINUTES_ARCHIVE=/Volumes/WD1/duckdb/snapshots/engine-a-minutes-archive
export FQUANT_SNAPSHOT_ROOT_ENGINE_A_TRANS_ARCHIVE=/Volumes/WD1/duckdb/snapshots/engine-a-trans-archive
export FQUANT_SNAPSHOT_ROOT_FSTORE_EXTENDED=/Volumes/WD1/duckdb/snapshots/fstore-extended
export FQUANT_SNAPSHOT_ROOT_ENGINE_A_MONEYFLOW_MINUTE=/Volumes/WD1/duckdb/snapshots/engine-a-moneyflow-minute
export FQUANT_SNAPSHOT_ROOT_ENGINE_A_CALLAUCTION=/Volumes/WD1/duckdb/snapshots/engine-a-callauction
export FQUANT_SNAPSHOT_ROOT_ENGINE_A_ORDERED_TRANS=/Volumes/WD1/duckdb/snapshots/engine-a-ordered-trans
export TICKFLOW_CANONICAL_HISTORY_ROOT=/Volumes/WD1/duckdb/snapshots/tickflow-canonical-history

# 可选：AI
export AI_PROVIDER=openai_compat
export AI_BASE_URL=https://api.deepseek.com/v1
export AI_API_KEY=...
export AI_MODEL=deepseek-chat
```

### 启动命令

```bash
cd backend
uv sync                              # 安装依赖
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

或前端一起跑（项目根）：

```bash
./dev.sh                             # 后端 3018 + 前端 3011
```

### 验证步骤

1. **Provider capabilities 检查**：
   ```bash
   curl http://127.0.0.1:8000/api/capabilities | jq .
   ```
   应返回 `realtime: true/false` 等布尔字段。

2. **FQuant provider 端到端测试**（独立运行）：
   ```bash
   cd backend
   uv run python scripts/test_fquant_provider.py
   ```
   预期无失败；真实 DuckDB 源不可达或缺数据时脚本会单独列 skip。

3. **健康检查**：
   ```bash
   curl http://127.0.0.1:8000/health
   # {"status":"ok","version":"x.y.z","mode":"none|free|api_key"}
   ```

4. **数据流验证**（dev 模式下手动触发）：
   - 设置页 → 「立即跑盘后管道」拉日 K
   - 自选页加标的 → 选股页跑策略扫描
   - 监控中心 → 配规则 → 命中看持久化记录

### 常见排错

| 现象 | 排查 |
|------|------|
| fquant/fquant_local 模式下接口返回空 | 检查 DuckDB 文件是否挂载、路径 env 是否正确、对应表是否有数据；客户端 fail-soft 返回空 df + warning |
| fquant/fquant_local 模式下 realtime 接口返回空或状态 `stale` | 检查 `fstore-markets.duckdb` generation 快照的 `daily_markets` 覆盖（确认 `snapshots/fstore/current.json` 已发布、`MAX(trade_date)` 为当前交易日）；若 API 有当日数据但 enriched 未更新，检查 `quote_service` 的 `enriched 计算失败` 日志 |
| fquant 模式下 depth 接口返回空 | 正常降级（当前 provider 不暴露 depth capability） |
| fquant_local 盘后管道不生成 `kline_daily` | 正常：stock raw mirror 被 repository 层禁写；只生成/更新 `kline_daily_enriched` |
| A 股 minutes/trans 返回空并出现 catalog warning | **staged catalog 是前置条件**：`require_current` 路由必须 `stage=preliminary`/`final`，旧 `stage=NULL` 行会被 fail-closed 拒绝并带可行动迁移指引（不降级 raw）。排查：catalog `current.json`、目标 root generation、路由是否为 staged；详见下方「catalog/engine 发布顺序」 |
| fquant_local 其它数据 freshness 落后 | 检查 `/Volumes/WD1/duckdb/fstore*.duckdb`、`/Volumes/WD1/duckdb/tdx*.duckdb` 是否更新 |

### catalog/engine 发布顺序（staged 迁移运维）

A 股 minutes/trans 是**日期分片**数据，必须经 `catalog_resolver.resolve_route` 解析，**刻意不降级 raw**。staged catalog 是前置条件：只有 `stage=preliminary`/`final` 的 `require_current` 路由能证明一次实时读该 pin 哪个 generation；旧 `stage=NULL` 行 fail-closed 拒绝，错误信息带可行动迁移指引。

**默认挂载根**：`/Volumes/WD1/duckdb`，所有 snapshot root 均在其 `snapshots/` 子目录下：

| root | 默认路径 | env 覆盖 | 用途 |
|------|---------|---------|------|
| catalog | `/Volumes/WD1/duckdb/snapshots/catalog` | `FQUANT_SNAPSHOT_ROOT_CATALOG` | 路由表（最先发布） |
| engine-a | `/Volumes/WD1/duckdb/snapshots/engine-a` | `FQUANT_SNAPSHOT_ROOT_ENGINE_A` | final require_current 快照 |
| engine-a-preliminary | `/Volumes/WD1/duckdb/snapshots/engine-a-preliminary` | `FQUANT_SNAPSHOT_ROOT_ENGINE_A_PRELIMINARY` | preliminary 快照（早发布，质量未校验） |
| engine-a-minutes-archive | `/Volumes/WD1/duckdb/snapshots/engine-a-minutes-archive` | `FQUANT_SNAPSHOT_ROOT_ENGINE_A_MINUTES_ARCHIVE` | pinned_immutable 历史归档 |
| engine-a-trans-archive | `/Volumes/WD1/duckdb/snapshots/engine-a-trans-archive` | `FQUANT_SNAPSHOT_ROOT_ENGINE_A_TRANS_ARCHIVE` | pinned_immutable 历史归档 |
| fstore-extended | `/Volumes/WD1/duckdb/snapshots/fstore-extended` | `FQUANT_SNAPSHOT_ROOT_FSTORE_EXTENDED` | extended 整库（财务三表）独立快照，与 fstore generation 隔离 |
| engine-a-moneyflow-minute | `/Volumes/WD1/duckdb/snapshots/engine-a-moneyflow-minute` | `FQUANT_SNAPSHOT_ROOT_ENGINE_A_MONEYFLOW_MINUTE` | tdx_moneyflow_minute 整库独立快照，与 engine-a generation 隔离 |
| engine-a-callauction | `/Volumes/WD1/duckdb/snapshots/engine-a-callauction` | `FQUANT_SNAPSHOT_ROOT_ENGINE_A_CALLAUCTION` | tdx_callauction 整库独立只读快照 |
| engine-a-ordered-trans | `/Volumes/WD1/duckdb/snapshots/engine-a-ordered-trans` | `FQUANT_SNAPSHOT_ROOT_ENGINE_A_ORDERED_TRANS` | 离线 raw trans 保序 materialization 的 per-symbol/day sparse true-trade Parquet；runtime provider 只读 hash-pinned generation |
| tickflow-canonical-history | `/Volumes/WD1/duckdb/snapshots/tickflow-canonical-history` | `TICKFLOW_CANONICAL_HISTORY_ROOT` | 面板生成的 A 股 canonical enriched 全历史；schema v2 原生保存复权前 `raw_open/raw_high/raw_low/raw_close`；首次全量任务固定 tdx/fstore/markets/klines/extended 具体 generation 路径，可用 1–8 个独立只读 worker，临时 staging DuckDB 只作聚合并在成功/失败后删除；盘后增量发布克隆 immutable 父代、复制已验证新日期分区并做 coverage/父代 CAS；完整成功后才原子切换 `current.json`，不写用户 `data/` |

**无中断发布顺序**（先数据后路由，避免读到未发布的物理文件）：

1. 先发布物理 snapshot root（engine-a / engine-a-preliminary / 各 archive），确保 `current.json` 指向新 generation；
2. 再发布 catalog root，写入带 `stage` 的新路由行（preliminary → final）；
3. 校验：`require_current` 路由的 generation 必须与对应 root 的 `current.json` 完全一致。

**安全回滚条件**：catalog 回滚到最后一个 generation 与物理 root `current.json` 仍一致的版本即可；若物理 root 已推进到更新的 generation，则 catalog 必须同步回退到 pin 该 generation 的路由行，否则触发 `StaleCatalogError`（fail-closed，不降级 raw）。preliminary 行可随时撤回而不影响 final 读取。

---

## 6. 不要做的事（红线汇总）

1. **❌ 不要重新引入 TickFlow SDK 或 `app.tickflow.*` 兼容层**
2. **❌ 不要在业务层或 FQuantProvider 内直接连接外部行情接口**（Tencent / 新浪 / 第三方）——FQuantProvider 保持只读本地 DuckDB；外部公共免费行情只允许走第 4 节「受控外部 fallback 契约」的适配层（默认关闭、仅补真缺口、provenance 标记、不污染主链路）
3. **❌ 不要改 `base.py` 接口契约**——除非同步新增 capability 字段并更新所有 provider
4. **❌ 不要假设 `DATA_PROVIDER=fquant` 一定有 depth 数据**；realtime 也要能处理本地源暂时返回空
5. **❌ 不要重新引入 fstore PostgreSQL 密码/HTTP 源依赖**——当前源只允许 DuckDB 只读文件
6. **❌ 不要改 `data_providers/normalizer.py` 字段语义**——所有 provider 共用同一规范化器
7. **❌ 不要删除 `backend/docs/FQUANT_INTEGRATION_PROGRESS.md`**——它是团队权威进度源
8. **❌ 不要直接 `git commit`** 本仓库的任何改动（除非用户明确授权）——所有改动由用户自行 review
9. **❌ 不要跑 `git clean -fdx` / `git reset --hard`**——会删光 `data/` 下所有未跟踪数据
10. **❌ 不要修改 `data/` 目录下的用户数据文件**——行情 / 财务 / 自选 / 回测 / 监控记录都是运行时生成的
11. **❌ 不要向 Pi 模型注册文件、命令、任意网络或直接 DuckDB 工具**——模型只能经 NDJSON `tool_request` 请求 Python 的 13 个只读 allowlist 工具；Node 进程环境必须继续使用 allowlist，不能恢复父进程全量环境继承

---

## 7. 维护说明

- **本文件**与 `FQUANT_INTEGRATION_PROGRESS.md` 同源，每次重大架构变更后两文件一并更新
- 改动 service 层时，**先看 `kline_sync.py`**（试点文件）；新增 service 时复制它的 `_get_data_provider()` 模式
- 改动 provider 时，**先看 `fquant/` 子模块**；研究专用 PIT 事实读取参照 `daily_market_research.py`，不得下沉到业务 service 直连 DuckDB
- commit 前**重新校对**「阶段 2：Service 层解耦」「阶段 3：补 FQuantProvider 缺口」两节的"已完成/待完成"标记
- 用户面向说明改 `README.md`；开发者面向说明改 `AGENTS.md` + `backend/docs/`

---

**最后更新**：2026-09-03（Issue #56 dataquery v2 cutover：A 股点查/窄区间切换 v2 HTTP、批量显式 blocked、`FQUANT_DATAQUERY_ENABLED` 可整链回退；此前 Research Workbench V2 完成 19 因子统一目录、preflight、Durable Run、11 项独立 full-market worker、不可变 artifact、证据关联与定时治理；不改变因子裁决且不自动进入策略池、Agent 或交易执行。）
**维护者**：tickflow-stock-panel contributors
**风格参考**：Hermes `~/.hermes/profiles/oc-hq/SOUL.md`（项目身份卡范式）
