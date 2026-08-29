# ISSUE-38 最终设计：四类持有形态独立研究

> 状态：**定稿**。本文吸收 [plan-v1](plan-v1.md)、[review-v1](review-v1.md)、[plan-v2](plan-v2.md) 与 [review-v2](review-v2.md)，是 Issue #38 的唯一实施契约。
> 修订（2026-08-27，coding review）：缺棒契约按“先证明父事件，再记录 selection/warmup/horizon censor”精化，避免在父形态本身不可观察时虚构分母；裁决与影响面已记录于 [Issue 评论](https://github.com/wf2311/fm-workbench/issues/38#issuecomment-5455139057)。

## 1. 边界与 interface

v1 仅做 A 股日频、只读、可审计研究。四个形态共享数据 pin、PIT 制度、执行和统计实现，但信号、分母、基准、IS/OOS 与 verdict 独立。不接前端、short pool、Agent、optimizer、监控或真实交易，不写 `data/`，不输出订单/买卖建议，不把“控盘/洗盘/主力”作为事实。

新增深模块 `backend/app/services/hold_firm_patterns/`；外部 interface 只有：

```python
def assess_capability(reader, market_facts, universe_reader) -> CapabilityResult: ...
def evaluate_hold_firm_patterns(request, reader, market_facts, universe_reader) -> HoldFirmResponse: ...
```

内部：`models.py` 冻结 Protocol/枚举/不变式；四个 detector 文件只从已排序 bars/PIT facts 生成父事件、landmark 和 qualification evidence；`evaluation.py` 独占 I/O 编排、执行、统计、删失和 verdict；`__init__.py` 仅导出 interface 与请求/响应类型。API 只校验、构造请求级 pinned adapters、调用并关闭资源。

## 2. 请求与固定常量

端点：`POST /api/research/factors/hold-firm-patterns/evaluate`，另有 capability GET。

```text
symbols: 1..200 个规范化 A 股 symbol
start < oos_start <= end
cost_bps: 默认 10，范围 0..1000
horizon: 20 market days
forward checkpoints: 1/5/10/20 market days
MIN_OOS_EVENTS: 30（每个比较组）
MIN_OOS_SYMBOLS: 10（每个比较组/holding 配对集）
BOOTSTRAP_SEED: 42
BOOTSTRAP_ROUNDS: 5000
CI: 95% percentile [2.5%, 97.5%]
Pydantic extra: forbid
```

所有固定数值均为本研究自定，不归因于原视频；写入 `definition_version=v1`、machine-readable definition 和 `params_provenance`。

## 3. 数据身份与价格口径

请求内固定：

1. `PublishedCanonicalDailyReader` 的 canonical generation、manifest sha256、source generations/hashes、calendar；
2. canonical manifest 指向的 `PublishedDailyMarketFactsReader` markets generation/manifest hash；
3. `PublishedPresenceUniverseReader` 的 schema/rule、published manifest SHA-256、retrospective source generation/manifest SHA-256，以及实际 membership day/content-hash identities。

required canonical columns：`symbol/date/open/high/low/close/raw_open/raw_high/raw_low/raw_close/volume/amount`。结构、均线、归一化收益用同 generation adjusted OHLC；raw OHLC 仅用于涨跌停与执行证据，字段分别命名 `research_*_adj` / `quote_*_raw`。

markets 参与日期的 row 必须完整提供 `raw OHLC/pre_close/published_limit_up/published_limit_down/regime/is_st/name`。缺 row、raw、band、regime、is_st 或身份/hash，整单 `unavailable_market_facts_incomplete`；不能猜停牌。`suspended/buyable/sellable` 不是输入要求。

可达性派生：

- `raw_open==raw_high==raw_low==raw_close==upper`（容差 0.005）为 entry unreachable；
- 同价等于 lower 为 exit unreachable/pending；
- raw OHLC 全相等但不在 band 是合法一价日；
- 所有制度价按两位小数与 `abs_tol=0.005` 比较。

PIT presence 仅有“可证明存在”与“不可证明”两种研究语义：published reader/manifest/artifact/hash/calendar/coverage 缺陷、非市场日、空源日，或 symbol 在 parent membership date 为 `NOT_OBSERVED`，均整单 `unavailable_universe_presence`；membership date 对有 landmark 的 parent 取 landmark date（F1 为首阴后第 1 日、F2 为突破后第 5 日），无 landmark 的删失 parent 才取 anchor date。只有 `PRESENT` 可进入 parent/censor/收益分母。presence 不证明 absence 是不在池或退市，因此 production `pit_universe_ineligible` 恒为空；禁止用 markets 聚合 universe、当前 instruments、请求 symbols 或 forward-only `eligible_v1` 回填。缺棒分两类：父事件本身所需的平台/缓坡窗口不完整时，父事件不可被证明，禁止伪造 parent/censor 分母；父事件已经由可见输入证明后，F2 day1..5、F3 低位回看、F4 底部回看或执行 horizon 缺失才进入对应 event censor。

## 4. Selection landmark 与共同执行时钟

每个父事件有一个 landmark。facts-complete parent events 必须互斥分成 `qualified` 与 `not_selected`；两组从 landmark 后同一精确市场日 raw open 开始同一 20 日观察时钟。

- F1 landmark：首阴后第 1 市场日收盘。
- F2 landmark：突破后第 5 市场日收盘；即使第 1 日已回踩也等至 day 5，早确认策略不由 v1 推断。
- F3/F4 landmark：信号当日收盘。

父事件已证明后的 landmark/selection window 不完整为 `censor_selection_window_incomplete`；F3/F4 已证明父形态后的低位/底部回看不完整为 `censor_warmup_incomplete`。entry 日缺 row/invalid open/一字涨停分别 censor，不向后找价。退出信号收盘确认，下一市场日起找第一可卖日；一字跌停为 pending，跳空按实际 raw open。

事件不变式：

```text
facts_complete_parent = qualified + not_selected
parent = facts_complete_parent + selection_window_censored
qualified ∩ not_selected = ∅
```

同因子同标的 active/pending 期间不重叠新事件；不同因子不互相去重。

## 5. 四个冻结因子

### F1 `first_yin_complement`

- 父事件：PIT 证明连续至少 3 个可交易涨停日；随后 1..5 日内第一根 `close_adj < open_adj` 的完成 K 线为首阴，之前不得已有阴线。
- 首阴结构：现场复算 `close_adj >= MA5_adj`。
- 首阴量态相对最后涨停日：`<=0.70` shrink，`>=1.50` expand，中间态不通过。
- landmark 日互补：shrink 首阴后 volume `>=1.50 × 首阴 volume`；expand 首阴后 volume `<=0.70 × 首阴 volume`。
- qualified：守 MA5 且互补；not_selected：facts 完整但任一未通过。父池递进统计另报全部首阴、守 MA5、破 MA5。
- dynamic defense：close 严格跌破现场复算 MA5，下一可卖日退出。
- 诊断：量态两桶、连续跌停日数、pending/无法卖出占比。

### F2 `breakout_pullback`

- 父事件：前 20 完整日平台，`(max(high_adj)-min(low_adj))/min(low_adj) <0.15`；当日 `close_adj > platform_high_adj`。
- 放量突破：volume `>=1.50 × prior_20d_mean_volume`。
- 1..5 日回踩：第一日满足 `low_adj <= level×1.01`、`close_adj >= level`、volume `<=0.70×breakout_volume`；`level=prior platform high`。
- qualified：放量突破且完整 day-5 窗内命中回踩；not_selected：完整窗口未命中。OLS `log(volume)` slope<0 仅诊断，不进 mask。
- dynamic defense：close 严格跌破 level，下一可卖日退出。
- 假突破：确认后独立完整 5 日窗口任一 close 严格低于 level；窗口不足为 diagnostic censor。

### F3 `low_gentle_slope`

- 低位：窗口开始前 120 日 `(close_adj-min(low_adj))/(max(high_adj)-min(low_adj)) <=0.35`，分母非正则 event censor。
- 20 日父缓坡：每日 close-to-close return 在 `[-3%,3%]`；`log(close_adj)` OLS slope>0、R²>=0.60；当日 close 严格创前 19 日新高。
- 阳线占比 `close_adj>open_adj` 至少 60%。缩量要求 `log(volume)` slope<0 且后 5 日均量 `<=0.80×` 前 5 日均量。
- qualified：低位父缓坡同时满足阳线占比与缩量；not_selected：facts 完整但任一未通过。
- dynamic defense：close 严格跌破 MA20，或 volume `>=1.50×` 前 20 日均量且 `abs(close/open-1)<=1%` 的放量滞涨；下一可卖日退出。
- `hypothesis_label="control_inference_unverified"` 只作说明；amount/volume 分位、零成交与流动性枯竭代理只作诊断，不进 mask。

### F4 `bottom_platform_breakout`

- 底部：平台起点前 120 日价格位置算法同 F3，`<=0.35`。
- 父事件：前 20 日平台振幅 `<15%`，当日 `close_adj > platform_high_adj`。
- qualified：同时满足底部、`close/open-1 >=5%`、volume `>=1.50×prior_20d_mean_volume`；否则 not_selected。
- dynamic defense：close 严格跌破突破日实体底 `min(open_adj,close_adj)`，下一可卖日退出。
- 独立完整 5 日诊断分母：`broken`（任一 close 跌破实体底）>`very_strong`（全程 close 不低于突破日 close）>`strong`（全程 close 不低于实体底）>`unclassified`。窗口不足为 diagnostic censor。
- 假突破：后 5 日任一 close 严格低于 platform high。

## 6. Holding 共同路径

仅在 qualified events 上比较 `dynamic_defense` 与 `fixed_hold_20d`。两臂共享 entry 和 day-20 终点：

- dynamic 实际退出后，扣费现金价值保持常数至 day 20；
- dynamic 破位但连续不可卖、到 day 20 仍 pending 时保持市场暴露，按每日日终 adjusted close 构造路径，day 20 mark-to-market；另记 `realization_censor_pending_exit`，不从 terminal/MAE/MFE 分母删除；
- fixed 始终按 adjusted close mark-to-market 至 day 20；
- terminal return、MAE/MFE 在共同 20 日路径上比较，holding days、pending days、实际 exit quote 单列。

费用在 entry 与真实 exit 各扣 `cost_bps`；day-20 mark-to-market 不虚构卖出费用，另提供 `liquidation_cost_adjusted_terminal_return` 诊断，verdict 固定使用后者以保证两臂成本口径一致。

## 7. 统计与 verdict

selection 比较 landmark/horizon 对齐的互斥 qualified vs not_selected；parent 只描述。两组各需 OOS complete events>=30 且 unique symbols>=10。重采样 union unique symbols 5000 次（seed 42），每次有放回抽取与 union 同数的 symbol clusters，保留每个抽中 cluster 的全部事件与抽中倍数，计算两组 event-weighted mean 差；任一组为空的 replicate 无效。有效 replicate<4750（95%）则 unavailable。CI 为有效差值的 2.5/97.5 percentile。下界>0 accepted，否则在样本门满足时 rejected。

holding 使用相同 qualified event_id 的 paired differences；需 events>=30、unique symbols>=10。按 qualified unique symbols cluster-resample，保留每个 cluster 全部事件对，分别对 liquidation-cost-adjusted terminal-return 差与 MAE 差生成 95% percentile CI。return CI 下界>=0 且 MAE CI 上界<0 才 accepted；任一明确失败 rejected；门槛不足 unavailable。

每因子总 verdict：selection 与 holding 都 accepted 才 accepted；任一 rejected 即 rejected；其余 unavailable。只用 OOS；IS 仅诊断。不得输出跨因子合并胜率或总排名。

## 8. 响应与 provenance

顶层 discriminator `status=ok|unavailable`、`definition_version=v1`。ok 时 `factors` 恰好四项，每项独立含 `parent_events/qualified_events/not_selected_events/segments/censored/denominator_audit/is/oos/diagnostics/selection_verdict/holding_verdict/verdict`。unavailable 时四因子结果与统计全部为空；只返回已验证身份，禁止部分可信结果。

provenance 必含 canonical/markets/universe identities、manifest hashes、required columns、calendar、definition/version、全部参数及来源、成本、horizon、bootstrap contract、代码版本、原稿证据路径。市场级 regime 无可信 pinned 来源时固定诊断 `unavailable`，不得用请求 cohort 冒充，也不得影响核心 mask。

## 9. 验证门

覆盖四检测器阈值/截断、10/20/30cm 与 ST、PIT presence PRESENT/NOT_OBSERVED/fail-closed、canonical/markets/presence pin 漂移、除权、T+1、一字涨跌停、pending 到 day20、跳空、费用、F1 两日互补、F2 day5 landmark/假突破、F3 OLS/低位/流动性、F4 强弱分母、互斥计数、共同时钟、cluster bootstrap 退化/空 replicate、IS/OOS 隔离、schema discriminator 与 reader lifecycle。focused/full backend tests、Ruff F/E9 和独立 coding review 无 blocker/major 后方可交付。

## 10. Issue #40 dependency addendum: presence_v1

生产 universe reader 改为 Issue #40 独立 `presence_v1` retrospective exact-day
published presence；不再依赖 forward-only `eligible_v1` SCD。presence 只能证明
`PRESENT`，不能证明 absence 是不在池或退市，因此 production
`pit_universe_ineligible` 恒为空；symbol 缺席、`NOT_OBSERVED`、缺 snapshot、
coverage、非市场日或完整性错误均使整单 `unavailable_universe_presence`。

`UniverseIdentity` 披露 rule/schema、`retrospective=true`、status filter、published
manifest canonical-JSON SHA-256、source generation/source manifest SHA-256，以及
实际请求 membership day/content-hash identities。
