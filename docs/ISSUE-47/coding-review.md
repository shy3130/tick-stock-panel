# Issue #47 编码复核

实现与冻结 detector/聚合契约一致，并已通过 pinned production reader 与 `POST /api/research/factors/pre-surge-features/evaluate` 接入。

已核对：

- detector 只消费显式 bars、PIT facts、benchmark 和 calendar；
- F2 `signal_date` 为确认日，不回填 gap 日；
- PIT ST 在 published ztj 缺失时仅以同日 regime/pre-close 校准，缺事实删失；
- F3/F4 暖机与基准缺口显式删失；
- 必要/充分分母分离，单因子和组合 verdict 独立；
- production 绑定 canonical/markets/universe identity，OOS 标签窗口与 T+1 reachability 明确；
- 无外部网络、文件写入、short_pool 或交易调用。

独立 review 未报告本 Issue 的 P0/P1/P2 问题。
