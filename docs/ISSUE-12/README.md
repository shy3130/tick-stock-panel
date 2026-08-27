# Issue #12 — 弱转强涨停事件因子

- Issue: https://github.com/wf2311/fm-workbench/issues/12
- 集成分支: `issue-8-research-production`
- 状态: `event-engine-ready; blocked-by-pit-and-orderbook-history`

文档：feasibility.md、plan-v1.md、review-v1.md、plan-v2.md、review-v2.md、final-design.md、verification.md。

完整 reader 注入后的日线/PIT/竞价/分钟/逐笔/盘口事件路径、bar-touched 降级和 OOS/成本摘要已实现。生产仍缺完整 PIT ST/股本历史与历史盘口，保持精确 unavailable/censored。
