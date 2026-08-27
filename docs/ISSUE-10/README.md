# Issue #10 — 15 分钟方向 + 5 分钟确认因子

- Issue: https://github.com/wf2311/fm-workbench/issues/10
- 分支: `issue-10-ordered-trans-bars`
- 状态: `productionized; real-verdict-rejected`

## 工作流记录

- [可行性](feasibility.md)
- [方案 v1](plan-v1.md)
- [一审](review-v1.md)
- [方案 v2](plan-v2.md)
- [二审](review-v2.md)
- [生产方案 v1](production-plan-v1.md) / [一审](production-review-v1.md)
- [生产方案 v2](production-plan-v2.md) / [二审](production-review-v2.md)
- [生产方案 v3](production-plan-v3.md)
- [真实尾盘口径修订](production-amendment.md)
- [最终设计](final-design.md)
- [验证记录](verification.md)

已接入 dedicated published ordered-trans generation：离线 publisher 从 raw CSV 按同分钟物理顺序生成 sparse true-trade 1m Parquet，runtime FQuantProvider 只读 hash-pinned artifact；消费端强制 48×5m/16×15m anchors、固定 OOS、global overlap purge 和同样本基线。真实三标的 30 日研究链路可运行，最终 verdict 为 `rejected`，未进入短线池、Agent 或默认策略。
