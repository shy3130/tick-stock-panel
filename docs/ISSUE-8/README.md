# Issue #8 — N 字金凤凰首板回调研究因子

- Issue: https://github.com/wf2311/fm-workbench/issues/8
- 状态: `production-engine-ready; blocked-by-pit-st-history`
- 集成分支: `issue-8-research-production`

## 工作流记录

1. 集成可行性：`feasibility.md`（可行，但 generation/PIT 缺口必须 fail-closed）
2. 方案 v1：`plan-v1.md`
3. 一审：`review-v1.md`（不通过）
4. 方案 v2：`plan-v2.md`
5. 二审：`review-v2.md`（两项修订已完成）
6. 最终设计：`final-design.md`
7. 编码与定向验证：已完成；真实事件仍由 PIT ST 历史缺口 fail-closed

## 范围

实现仅用于研究的 `n_shape_golden_phoenix_v1` 日线事件因子：从可审计的低位首板、2–10 个交易日缩量回调、结构保持到独立二次启动变体，返回证据、删失/不可用原因及研究结果。

## 非目标

- 不改变 `short_momentum_quality_v1` 的固定筛选、排序、Agent 权限或默认候选。
- 不输出交易建议、不接真实交易、不写交易事实流。
- 不使用外部行情或非 sealed 数据；不在数据/样本外结论不足时降低门槛。

## 验收标准

1. 每个事件状态仅使用信号日可见的 canonical 历史数据，含版本、generation 与 manifest 字节哈希证据。
2. 数据不足、PIT 制度/ST 证据缺失、停复牌、一字板或无法证明成交时显式删失/不可用。
3. 与同一日线价格定义下的全部首板基准比较 OOS 增量、成本、置信区间及失败分层；未达门槛即 `rejected`。
4. 覆盖普通/低位首板、缩量失败、结构位跌破、一字板、二次放量、二次涨停回封、停复牌及缺数据夹具。
5. Agent 只能解释返回证据，不能增删或重排候选。

## 文档清单

- [可行性评估](feasibility.md)
- [方案 v1](plan-v1.md)
- [一审](review-v1.md)
- [方案 v2](plan-v2.md)
- [二审](review-v2.md)
- [最终设计](final-design.md)
- `verification.md`（实现和验证完成后填写）
