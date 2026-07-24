# 个股详情当日实时蜡烛独立复核

状态：通过（2026-07-21）。

复核对象：`REQ-STOCK-DETAIL-REALTIME-CANDLE-001`。

## 需求到证据复核

- 实时 OHLCV/成交额：`backend/app/api/kline.py::_maybe_inject_live_candle` 从当前配置的 realtime provider 按 symbol 读取并合并；NBIS 生产容器实值验证通过。
- 昨收回退涨幅：provider 缺少 `prev_close` 和 `change_pct` 时使用上一根日线 close 计算；NBIS 的 177.71 基线验证通过。
- 市场交易日：行情时间戳使用 `market_rule_for_symbol(symbol).timezone` 转换；美股跨北京时间午夜仍生成纽约日期 2026-07-20。
- 历史隔离：请求结束日期早于市场当前日期时在读取 provider 前返回；可执行测试验证 provider 未被调用。
- 字段安全：无效实时价格不生成蜡烛，缺失 open/high/low 延续既有 close 回退语义。

结论：实现、可执行测试与生产语义证据均覆盖权威需求，未以快照或下游指标替代底层行情字段验收。
