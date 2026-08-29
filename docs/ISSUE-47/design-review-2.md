# Issue #47 设计评审二：分母与 verdict

## 结论

采用必要/充分双向统计，但只有充分方向进入 verdict。

- 必要方向：`P(feature | future surge)`，仅描述大涨样本中形态覆盖。
- 充分方向：`P(future surge | feature)`。
- 基线：同一评估样本的无条件 `P(future surge)`。
- lift：充分率 / 无条件率。

## 准入门槛

每个单因子和组合分别要求至少 30 个命中样本、lift 至少 1.15，且充分率相对无条件率的近似 95% 置信区间下界大于 0；否则分别为 `unavailable` 或 `rejected`。该近似区间用于保守阻止过度宣称，不替代后续按标的聚类 bootstrap。

## 决策

响应固定 `promoted=false`；未完成冻结 OOS 前不得进入 short_pool 或 Agent 排序。
