# Issue #46 Final Design

## 公开接口
`PinnedFactorPanel`、`build_pinned_factor_panel`、`RetrievalRoutingRequest`、`RetrievalRoutingResponse`、`evaluate_retrieval_routing`、`assert_neighbor_boundaries`、`select_routing_config`；API 为 `POST /api/research/factors/mera-routing/evaluate`。

## 算法契约
日期按唯一升序切 60/20/20；特征标准化、forward-return 三分位标签、检索库仅使用标签在 train 结束前完全兑现的 train 行。候选 K={5,10,20}、距离 euclidean/cosine 只用标签在各自 split 结束前兑现的 train+validation 行选择，test 冻结；基线特征选择遵守同一 purge。邻居必须来自 train，且其 `label_available_date = neighbor_index + label_horizon` 严格早于 `query_date`；仅有 `neighbor_date < query_date` 不足以防多日标签泄漏。

## 门禁与状态
至少 30 标的；validation/test 每个评估日至少 20 个 feature 与标签均完整的样本；三段日期非空；检索库至少可满足最大 K。失败返回 `unavailable_panel_coverage`。标签尾部 censor，不伪造未来收益。

## 审计输出
响应固定包含 schema、status、definition_version、request、identity、coverage、censored、events、verdicts、promoted=false；事件含邻居日期、label 可得日期、标的、距离、标签、路由类别和熵；provenance 记录 train 冻结参数和选择结果。多空换手以新旧等权权重向量的 L1 变化计算，池缩小时必须计入卖出及再平衡成本。RankIC 增量与成本后增量不得引用论文分钟基线。

## 安全边界
核心 evaluator 无文件写入、网络和外部行情；production builder 只接收已 pin 的 reader。安慰剂固定种子，异常增益阻断 verdict。模块不接默认池、不产生交易建议。
