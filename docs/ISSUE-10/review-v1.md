# 方案 v1 Review

结论：不通过。现有分钟接口只返回 price/volume，并在规范化时重建相同 OHLC，无法支持真实 15m 分型、长上影、ATR 和缺 bar 校验；catalog route 也未按运行固定。`MinuteExecutionData`/`fill_reachability` 是成交诊断，不是方向因子状态输入。另需按真实标签区间处理跨 session 的前瞻重叠。

修订动作：把真实时间戳、独立 OHLC、minute_index、session 完整性与 immutable route manifest reader 列为硬前置；缺失即 unavailable；移除复用成交诊断的承诺；记录 label interval/bar ids 并按区间去重和 split purge。
