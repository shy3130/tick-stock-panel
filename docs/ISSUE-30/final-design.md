# ISSUE-30 plan-v3：最终实施契约草案

关联：[Issue #30](https://github.com/wf2311/fm-workbench/issues/30) · [README](README.md) · [feasibility.md](feasibility.md) · [plan-v1.md](plan-v1.md) · [plan-v2.md](plan-v2.md) · [review-v1.md](review-v1.md) · [review-v2.md](review-v2.md)  
状态：**已批准（review-v3 approve；P2 已按 review-v3.md 处置）**。本文逐条修复 review-v2 的 M1–M3；未修订条款继续以 plan-v2（及其上游 v1）为准。本文的权威整合版为 [final-design.md](final-design.md)。本波不改代码、不运行测试。

## 0. 修订总览

| review-v2 finding | v3 修复 |
|---|---|
| M1：卖出侧只有聚合计数，无法归属候选 | 每个 candidate 单独调用一次 `simulate_independent_candidates`；服务以单候选返回结果生成终态 ledger，含 post-entry `sell_suspended`/`sell_limit_down` |
| M2：engine 的 open_t+1 shift 与锁定 index 矛盾 | 服务以 pinned calendar 锁定 T+1，并构造 T→T+1 连续单 symbol panel；只有下一行已经验证为 T+1 才使用既有 shift；不改公共 API |
| M3：raw PIT bands 与 adjusted panel 跨尺度比较 | raw bands 只与 raw OHLC 比较生成 bool flags，随后附加到 adjusted panel；增加 corporate-action fixture；markets pin 来自 manifest `source_generations['markets']` |

## 1. R1''：raw 尺度的 PIT 涨跌停带

### 1.1 两套尺度不可混用

现有 markets reader 的 `limit_regime_facts`（`daily_market_research.py:160-185`）暴露 raw `limit_up_price/name/is_st/regime`，不暴露 `pre_close`/lower band。v3 要求先在 **raw price scale** 完成事实重建和比较：

- `upper_raw = ROUND_HALF_UP(pre_close_raw × (1 + regime_ratio), 0.01)`；
- `lower_raw = ROUND_HALF_UP(pre_close_raw × (1 - regime_ratio), 0.01)`；
- `signal_limit_up = raw_high >= upper_raw`；`signal_limit_down = raw_low <= lower_raw`；一字板预检使用 `raw_open/raw_high/raw_low` 与 raw band；
- 以上 bool flags 生成后，才附加到供 engine 使用的**前复权 adjusted panel**。engine 的同一 bar `same_price` 检查（`engine.py:930-942`）在 adjusted bar 内部执行，不再将 raw 数值混入 adjusted 比较；锚、MA、决策价、成交价、pnl 仍使用 adjusted canonical OHLC。
- published `limit_up_price`（ztj）与 `upper_raw` 必须一致；不一致整单 `unavailable`。不把 raw limit price 直接拿去比较 adjusted high/low。

制度比例和生效日沿用 `daily_market_research.py:148-158`：`main_10=10%`、`st_5=5%`、`chinext_20=20%`、`star_20=20%`、`beijing_30=30%`。缺 `pre_close_raw`、regime、board、PIT ST 状态、raw OHLC 任一必需事实 → 整单 unavailable，绝不填 False 或降级无约束。

### 1.2 markets generation 唯一 pin

canonical manifest 必须提供 `source_generations['markets']`（manifest 的 `source_generations` 结构见 `canonical_history.py:449-452,685-692` 与 `backtest/provenance.py:27,66`）。此值是本次研究唯一 immutable markets generation 身份：

- canonical generation 与 markets generation 在请求开始时一并 pin；reader 禁止跟随 `current.json`；provenance 同时记录两者。
- key 缺失、值为空/歧义、无法打开该 generation、或 reader 返回的 generation 与 manifest pin 不一致 → 整单 unavailable。
- private research reader 契约增加 `limit_band_facts`，返回 `pre_close_raw`、published upper、regime、ST/board 事实、raw limit flags 所需字段；若底层 markets generation 不能提供 raw `pre_close`，必须报 unavailable，不改成当前价/相邻 close 猜测。

**测试新增**：corporate-action fixture 构造 raw bar 与 adjusted bar 具有不同比例；断言 raw `raw_high/raw_low/raw_open` 与 raw bands 得到正确 flags，并证明直接用 adjusted high/low 比 raw band 会产生不同结果；另测 manifest markets pin 缺失、generation mismatch、upper 交叉校验失败及所有 regime/ST 边界。

## 2. R2''：每候选一次 engine 调用与终态 ledger

### 2.1 不改公共 BacktestEngine API

v3 不增加参数、不改变 `BacktestEngine.simulate_independent_candidates` 公共签名。审查确认买入阻塞通过 `_count(block_reason)` 聚合（`engine.py:1068-1071`），卖出阻塞通过 `_try_close` 聚合（`engine.py:1008-1017`）。因此服务采用**一次调用只放一个 candidate** 的隔离方案，而不是要求引擎增加字段。

### 2.2 调用与终态映射

对每个通过服务预检的 `(symbol, signal_date, arm)`：

1. 服务生成该 candidate 的连续单 symbol panel（见 §3），entries 仅含该一个 signal，exits 使用该 symbol 已计算的死叉 bits。
2. 服务调用一次 `simulate_independent_candidates(panel, entries, exits, MatcherConfig)`；四臂分别调用，故四臂 × candidate 次调用。
3. 将该次返回的唯一 trade（若有）或唯一 aggregate execution reason 归入该 candidate 的**终态 ledger**；禁止用多候选 aggregate stats 猜分配。

终态 ledger 至少含：`symbol, signal_date, arm, planned_execution_date, filter_retained, precheck_status, terminal_status, terminal_reason, exit_reason, entry_price, exit_price, pnl_pct, mae_pct, mfe_pct, blocked_exit_days`。

映射冻结为：

- 有 `TradeRecord`：`terminal_status=traded`，记录 `exit_reason/pnl/MAE/MFE`；
- 入场前服务预检失败：`blocked` 或 `censored`，原因 `t1_bar_missing/suspended_no_bar/buy_limit_up/invalid_open/horizon_data_gap`，不调用 engine；
- 入场后 `_try_close` 阻塞：`sell_suspended` 或 `sell_limit_down`，保留 `pending_exit` 与 `blocked_exit_days`（`engine.py:1008-1017`）；
- `sell_no_future`（`engine.py:1169-1171`）在连续 horizon 契约下不可达（`last_idx != entry_idx`），不作 terminal outcome，也不设测试；
- engine 在最后 bar 以 `end` 收口：记录 `terminal_reason=end`，不得伪称 signal/max-hold；
- engine aggregate 与单 candidate ledger 不一致，或一 candidate 出现多个互斥 terminal outcome：整单 unavailable。

none 臂 ledger 是虚拟结局唯一来源；过滤臂按 `(symbol, signal_date)` one-to-one join。被过滤样本若 none 已成交，复制 candidate-sample 净收益/退出/MAE/MFE；none blocked/censored 则原样携带终态原因。

### 2.3 成本、规模与计算量

冻结 `MAX_SIGNALS_PER_REQUEST=1000`，symbols 仍 ≤200、窗口仍 ≤370 trading days；超过 signals 上限返回 400，要求分页。每个 signal 要跑四次（四臂各一次），每次扫描至多 16 个 market-day horizon rows，调用成本约为 `O(4 × n_signals × horizon_rows)`，不宣称组合级吞吐或净值。相同 panel 构造可复用，但每个 candidate 的 engine 调用与 ledger 终态必须隔离。

## 3. R3''：pinned calendar、连续 panel 与精确 T+1

### 3.1 锁定执行日

服务用 pinned canonical `market_days` 找 `T` 之后的第一个交易日 `T+1`；不使用 engine 的相邻行推断。对每个 candidate 构造单 symbol、按 pinned market-day 连续排列的 panel：至少包含 `T` 行和经过验证的 `T+1` 行，且 horizon 内每个 market day 都有同 generation canonical bar。entries signal bit 置于 T 行，下一行必须正是已验证 T+1，才允许既有 `entry_fill="open_t+1"` shift。

因此 engine 的既有 shift（`engine.py:815-820`）在这个隔离 panel 上不再改变语义：它只把 T bit 移到已经验证的 T+1 行。不得把 `engine_entry_index` 当作 engine 支持的执行 index，也不得把 T 后首个可见 bar 猜成 T+1。

### 3.2 调用前删失

下列情形不调用 engine，直接写 candidate ledger：

- T+1 没 canonical bar、停牌导致无 bar、PIT facts 不完整：`blocked/censored=t1_bar_missing` 或 `suspended_no_bar`；
- T+1 raw open 非法：`censored=invalid_open`；
- T+1 raw open/high/low 已构成一字涨停：`blocked=buy_limit_up`；
- 入场后的 required horizon（T+1 至 T+1+15，共 16 个 market days）任一 market day 缺 bar：`censored=horizon_data_gap`。

停牌只做诚实 censor，不创建占位 OHLC，不伪造成交；horizon 后续不把下一可见 bar 当作连续日。若 T+1 有效但后续 horizon 有 gap，仍在调用前记 `horizon_data_gap`，确保 engine 从不在非连续 panel 上运行。

死叉 exits 由 pinned adjusted close 的全 symbol 序列先计算，再投影到 candidate panel；止损和 15 日 max hold 由既有 MatcherConfig 执行。候选 panel 非连续、交错 symbol 或 T+1 缺 bar 的测试必须证明服务拒绝调用，而非延迟入场。

## 4. R4/R5 继承与响应契约

plan-v2 §4 的 `arms.*.segments.is/oos` 继续有效，按 signal_date 归段，verdict 只读取 OOS。plan-v2 §5 的 candidate-sample 白名单继续有效，但必须使用本 v3 终态 ledger 的分母：`n_signals/n_retained/n_filtered/n_candidates_executed/n_trades/stop_hit/avg_mae/avg_mfe/net_pnl_pct_mean/blocked_counts/censored_counts`；不得恢复组合 turnover、cost_total 或 portfolio MaxDD。provenance 新增 `markets_generation`、`source_generations_markets`、`execution_ledger_version=2`。

PR #32 后补充（2026-08-28）：响应顶层追加只读 `tnt_open_anchor_contrast`（来源 `scripts/tnt/`、`read_scope=oos_only`、日频个股 5 日趋势桶代理，不复现盘中做T研究）：对 `single_side_down`/`range` 桶披露 none/original 的 `n_trades`、`stop_hit_rate`、`expectancy` 与 `improved|adverse|neutral|inconclusive` 状态（任一臂 `n_trades < MIN_OOS_TRADES` 即 inconclusive）；verdict 据此追加 `applicability`（rejected → `not_applicable_rejected`；inconclusive → `inconclusive_overall`；仅 validated 时任一桶 inconclusive → `inconclusive_by_trend`、双桶 adverse → `unsupported_in_preregistered_regimes`、单桶 adverse → `conditional_by_trend`、双桶均 improved/neutral → `all_regimes`）与 `warnings`，既有 `label` 规则不变；该诊断为纯只读投影，不得回灌过滤 mask。

## 5. 增量测试矩阵

- raw/adjusted corporate-action fixture：raw bands 与 raw OHLC 正确，adjusted 直接比较被证明不采用。
- manifest `source_generations['markets']` 缺失、空值、generation mismatch、markets reader 无 pre_close：整单 unavailable。
- main/ST/创业板/科创板/北交所比例与生效日期、Decimal ROUND_HALF_UP 边界、published ztj 交叉校验。
- 每 candidate 恰好一次 engine 调用；四臂调用隔离；post-entry `sell_suspended`、`sell_limit_down`、`pending_exit/blocked_exit_days` 均能进入该 candidate 终态 ledger。
- T+1 缺 bar/停牌/invalid open/一字涨停/horizon gap：调用计数为零，ledger 原因精确，禁止使用下一可见 bar。
- 连续单 symbol panel：T bit + 验证 T+1 下一行；非连续 symbol panel 不得模拟。
- MAX_SIGNALS_PER_REQUEST=1000 边界与每候选四次调用的成本说明。
- IS/OOS segments、candidate-sample 分母、none 虚拟结局、OOS-only verdict 与 v1/v2 既有夹具保持一致。

## 6. 最终门禁

review-v2 的 M1–M3 已由 review-v3 确认；P2 已按连续 horizon 预删失规则处置，`final-design.md` 自本波起为实现唯一依据。实现波次仍须遵守 sealed-only、PIT fail-closed、T+1、A 股多头、无 `data/`、无订单/方向建议等红线；本波不改代码、不跑测试。
