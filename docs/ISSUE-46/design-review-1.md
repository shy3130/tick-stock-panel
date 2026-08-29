# Issue #46 Design Review 1

## 复核范围
复核公开请求/响应 envelope、PinnedFactorPanel seam、时序切分、PIT 邻居、train-only 拟合与日频代理边界。

## 结论
通过，保留 `schema/status/definition_version/request/identity/coverage/censored/events/verdicts/promoted=false`；K={5,10,20} 与 euclidean/cosine 为定义级候选集，选择不触碰 test。

## 风险决议
- 论文分钟 MERA 指标不得作为基线；改用面板内冻结的日频单因子基线。
- 研究不足返回 `unavailable_panel_coverage`，不降级为部分结果。
- 邻居不属于 train、`neighbor_date >= query_date`，或 `label_available_date >= query_date` 均必须报错/不可用，不静默保留；多日 forward label 必须在 query 前完整兑现。
