# ISSUE-38 设计评审 v2

结论：**Changes requested，首轮问题已全部关闭**。评审针对 [plan-v2.md](plan-v2.md)。

## 已关闭

- `suspended/buyable/sellable` 恒 unavailable blocker：已改为 raw/band 派生。
- F1/F2 selection 时间错位：已使用固定 landmark。
- 父池包含命中池：已改为互斥 qualified/not_selected。
- holding 提前退出机械改善 MAE：已使用共同 20 日现金路径。
- PIT universe 三态与 F4 五日诊断分母：已冻结。
- F2 day-5 landmark 不存在 immortal-time bias；整单 markets-facts 门在只覆盖参与评估的 canonical 行时合理。

## 剩余 Major

1. **独立标的数门槛缺失**：30 个 segment 可能全来自一个 symbol，cluster bootstrap CI 会退化。最终设计增加两侧/配对样本的最小 OOS unique symbols 门槛。
2. **cluster-bootstrap 不可复算**：需冻结 cluster 采样方式、cluster 内加权、轮数、种子、百分位和空组 replicate 处理。
3. **dynamic 到终点仍 pending 的估值缺失**：若连续一字跌停至 day 20，不能把最差路径排除。最终设计必须按 day-20 adjusted close 计共同路径 terminal/MAE/MFE，并单列 realization censor。

## 处理决定

全部接受。最终设计冻结 `MIN_OOS_SYMBOLS=10`、seed 42、5000 rounds、95% percentile cluster bootstrap；对 union-symbol clusters 重采样并保留 cluster 内全部事件，invalid replicate 比例超过 5% 时 verdict unavailable。dynamic pending 到共同终点时保持市场暴露并 mark-to-market，不从统计分母删除。
