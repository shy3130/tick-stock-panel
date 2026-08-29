# ISSUE-45 二轮设计复核

## 复核结论

冻结按 market calendar 对齐的窗口；缺失 bar 不被插值。T2∧T3 是 parent event，T1 与可选 M3 形成 qualified/not-selected 分层，确保事件分母可审计。

## 风险处理

- MA200 不足：`censor_warmup_incomplete`。
- signal 后 horizon bar 不足：`censor_horizon_incomplete`。
- OOS 事件或标的不足：verdict `unavailable`。
- 足量 OOS 但 95% 下界不为正：`rejected`。
- `promoted` 永远为 false。
