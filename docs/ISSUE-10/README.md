# Issue #10 — 15 分钟方向 + 5 分钟确认因子

- Issue: https://github.com/wf2311/fm-workbench/issues/10
- 分支: `issue-10-mtf-direction`
- 状态: `direction-engine-ready; blocked-by-true-minute-ohlcv`

## 工作流记录

- [可行性](feasibility.md)
- [方案 v1](plan-v1.md)
- [一审](review-v1.md)
- [方案 v2](plan-v2.md)
- [二审](review-v2.md)
- [最终设计](final-design.md)
- `verification.md`（实现和真实数据探针）

15m/5m 聚合、确认分型/ATR 斜率、5m 同反向确认、forward/MFE/MAE、基准和 IS/OOS 分层已实现。当前 sealed `market_minutes` 只有 `price/volume`（无真实 open/high/low/close），不能注册为生产 reader；服务因此继续 fail-closed。
