# 个股详情当日实时蜡烛验收

状态：通过（2026-07-21）。

验收对象：`REQ-STOCK-DETAIL-REALTIME-CANDLE-001`。

## 证据

- `backend/tests/test_stock_detail_realtime_candle.py`：2 项通过，覆盖实时 OHLC/最新价/成交量/成交额、昨收回退涨幅、纽约交易日和历史区间隔离。
- 相关回归：`test_market_overview_realtime.py`、`test_intraday_index_quotes.py` 与本需求测试共 6 项通过。
- 生产容器语义验收：NBIS.US 实时行归属 `2026-07-20`，返回 open `186.57`、high `194.34`、low `179.09`、实时 close `183.11`，以前一日 close `177.71` 补算 change_pct `0.0303865849`。
- 后端全套测试：490 项通过；1 项既有 `test_history_strategy_monitor_keeps_live_row_with_exclude_st_enabled` 因固定日期漂移失败，与本需求修改文件和执行路径无关，单独复跑结果一致。
