# Issue #10 — 15 分钟方向 + 5 分钟确认因子

- Issue: https://github.com/wf2311/fm-workbench/issues/10
- 分支: `issue-10-mtf-direction`
- 状态: `final-design-ready-for-implementation`

## 工作流记录

- [可行性](feasibility.md)
- [方案 v1](plan-v1.md)
- [一审](review-v1.md)
- [方案 v2](plan-v2.md)
- [二审](review-v2.md)
- [最终设计](final-design.md)
- `verification.md`（实现后填写）

当前生产环境缺少真实 generation-pinned 分钟 reader；实现必须稳定 fail-closed，不得把重建 OHLC 当成可审计分钟事实。
