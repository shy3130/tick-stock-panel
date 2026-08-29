# 可行性评估

## 结论

`eligible_v1` 的历史回填不可行；独立的 `presence_v1` 从 2022-03-04 起可行。

## 数据证据

- `base_infos` 仅是当前快照，只有 `ssdate`、没有可用退市历史；倒灌会产生幸存者偏差。
- `base_infos_history`、change records、`instrument_info` 的时间范围或数据量不足，不能覆盖目标历史。
- pinned `fstore-markets.daily_markets` 的 A 股历史覆盖 2022-03-04 至当前源末日，并保留之后退场标的的历史行。
- TDX 当前宽表已剪除部分退市标的，不能作为历史 PIT universe。
- 2020—2022 的成交 K 线在停牌日缺棒，不能冒充 eligibility/presence 日级事实，本期不纳入。

## 语义裁决

首轮 review 证明 first/last seen 推断会引入后验移出和尾部 absence 歧义，已废弃。`presence_v1` 改为严格同日事实：

- membership 仅由冻结 generation 中 `(asset_type=1, code, trade_date=event_date)` 行是否存在决定；
- 不读取 event date 之后的行，不 carry-forward，不从 absence 推断退市；
- coverage 外或非市场日为 unavailable；coverage 内缺行表示 `not_observed`，不等价于 eligibility 的 `NOT_IN_POOL`；
- 这是按当前冻结 source generation 重建的回顾性 presence，只用于研究 denominator，禁止用于 live/decision-time eligibility；
- manifest 记录 source generation/hash、coverage、source publication time 和 market-day digest。

## 架构边界

- 使用独立 schema v2 snapshot root/current，不与现有 `eligible_v1` ledger 混写。
- 每个 interval 显式指向同代 `symbols/<content_hash>.json`，reader 逐 artifact no-follow/hash/count 校验。
- 使用独立 `presence_symbols/prefetch_presence_days` interface；不得复用 `eligible_symbols` duck-typed seam。
- 现有 `eligible_v1` schema v1、publisher、reader 和 repository 属性保持零行为变化。
