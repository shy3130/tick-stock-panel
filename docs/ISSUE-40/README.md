# Issue #40 — PIT Universe presence history

- GitHub: https://github.com/wf2311/fm-workbench/issues/40
- 状态：实现、独立复核、全量验证与 production artifact 发布已完成；待 PR 合并及 Issue #38 消费接入
- 分支：`issue-40-pit-universe-presence`
- 依赖方：Issue #38

## 范围

从 pinned `fstore-markets.daily_markets` 构建 2022-03-04 起的回顾性 `presence_v1` Universe SCD。该规则只表示“冻结 source generation 在 event date 存在该 A 股行”，与 forward-only `eligible_v1` 明确分离，不用当前股票池或未来日期推断历史成员。

## 验收标准

以 GitHub Issue 正文为准；核心门禁是 source generation/hash/coverage/calendar pin、exact-day 语义、规则与 interface 隔离、self-contained artifact、原子发布和 Issue #38 可消费。

## 文档

- [可行性评估](feasibility.md)
- [方案 v1](plan-v1.md)
- [首轮方案 review](review-v1.md)
- [调整方案 v2](plan-v2.md)
- [二次方案 review](review-v2.md)
- [最终冻结 review](review-final.md)
- [最终设计](final-design.md)
- [Coding review](coding-review.md)
- [验证记录](verification.md)
