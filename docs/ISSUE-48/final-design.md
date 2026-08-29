# Issue #48 冻结方案

## 检测契约

模块提供 `EscapeS1Detector`、`EscapeS8Detector`、`EscapeS9Detector`，均返回 `daily_event_research.models.Detection`。

|信号|冻结口径|available 时点|
|---|---|---|
|S1|当日 high > 前 60 根 high 最大值，且当前红柱段截至当日的峰值 < 前一完整红柱段峰值；MACD EMA12/26、DEA9、hist=`2*(DIF-DEA)`|close|
|S8|最近三根均 `close < open`；doji 打断|close|
|S9|`(上一根原始收盘-当日原始开盘)/上一根原始收盘 >= 0.05`，等号成立；原始价格缺失时 censor|当日 open|

每个 evidence.values 包含 ISO `available_date`、条件原始值与持仓资格。S9 标注 `existing_position_required=true`；S1/S8 为 false。

## 聚合

`aggregate_escape_signals` 接收 evidence detections、按 symbol 的 bars、`horizons=(1,3,5,10)`、成本、显式基线和可选 benchmark。S1/S8 收盘确认后以下一交易日开盘为执行锚；S9 以信号日开盘为执行锚；N=1 表示执行日收盘。成本为两腿 round-trip。每个信号独立输出卖飞率、卖飞收益均值与下跌事件最大回撤；窗口不足计入 `horizon_incomplete_events`。

buy_hold 由 bars 的持有定义得到；MA20、ATR、prev_close 缺失时输出 `unavailable_no_baseline`。未冻结真实 OOS 结构化基线时 verdict=`unavailable_no_frozen_oos_baseline`，不得以样本内均值宣称接受；要求 benchmark 但未提供时为 `unavailable_benchmark_missing`。多信号仅输出计数 bucket，不输出方向指令。

## capability

S1/S8/S9 为 `available`；S2-S7、S10 为 `unavailable_insufficient_immutable_history`。`require_daily_signal` 与 `minute_approximation=True` 均 fail-closed；不接受日线 high/low 近似分钟路径。

能力边界通过 `GET /api/research/escape-risk` 暴露；日线评估 API 为 `POST /api/research/factors/escape-risk/evaluate`。
