# ISSUE-45 冻结设计

## 公开接口

`DailyEventRequest`、`DailyEventResponse`、`Detection`、`DailyEventDetector`、`evaluate_daily_events`；独孤趋势公开 `DuguTrendConfig`、`DuguTrendDetector`、`DUGU_VARIANTS`，API 为 `POST /api/research/factors/dugu-trend/evaluate`。

## 独孤趋势定义

- T1：signal day 的 fast MA > mid MA > MA200，且收盘站上 fast MA。
- T2：signal 前 10 个交易日内，low 进入 fast/mid 回调带；fixed 带宽为 3%，ATR 带宽为 1×ATR20。
- T3：signal close 站上 MA5，且此前 10 日存在收盘不高于 MA5 的日子。
- M3（可选）：signal close 相对 20 个交易日前收盘的涨幅不超过 30%，用于排除已经过度加速的样本。

所有指标只看 signal day 及之前的 calendar-aligned bar。MA200 暖机不足不伪造信号。

## 研究统计

事件在 signal 收盘确认后以下一交易日开盘为入场锚，持有冻结 horizon 后收盘退出；买卖两腿分别扣 `cost_bps`，一字涨停买不到、一字跌停卖不出与 PIT 市场事实缺失均删失。OOS verdict 比较 qualified 与同 detector 的 not-selected 基线，双方至少 30 个事件且各覆盖至少 10 个标的；成本后增量 95% 近似下界大于零才 accepted，否则 rejected，样本不足 unavailable。输出始终 `promoted=false`。
