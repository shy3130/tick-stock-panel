# Issue #48 设计评审二

## 复核决议

- `Detection` 继续使用 shared models；窗口不足复用 `CensorReason.WARMUP_INCOMPLETE`，PIT 昨收缺失使用模块枚举 `censor_pit_fact_missing`。
- 需要基准的研究比较若没有显式 benchmark，标记 `censor_benchmark_missing`/`unavailable_no_benchmark`，不填零。
- buy_hold 仅按给定 bars 的 signal-close 到 N 日后 close 定义；MA20、ATR、prev_close 只接受调用方预计算的 `BaselineSeries`。
- 成本固定为一进一出两腿；前向方向按原始收益判定，净收益单独扣成本。
- 同日多信号只按不同 signal 数量建 bucket，不生成合并方向或交易动作。

## 评审结果

通过。minute approximation 入口显式拒绝，所有 N 日窗口不足均计入 `horizon_incomplete_events`。
