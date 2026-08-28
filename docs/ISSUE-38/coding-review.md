# Issue #38 实现代码复核

## 结论

独立 reviewer 最终结论：**批准；未见 blocker/major**。

复核范围包括四检测器、共同执行路径、PIT universe/markets pin、IS/OOS 与 cluster bootstrap、API 生命周期、契约测试和本目录冻结设计。reviewer 未代跑测试；测试证据见 [verification.md](verification.md)。

## 首轮 findings 与处置

1. **F2 day1..5 缺棒被跳过**：改为父事件已证明后的 `censor_selection_window_incomplete`。
2. **同因子同标的事件重叠**：评估器按 symbol/landmark 排序；前一事件共同 horizon 内的新事件以 `censor_same_factor_symbol_overlap` 删失；允许在前事件 day20 收盘形成新 landmark、次日入场。
3. **F4 低位参考价格取错**：改为平台首日 adjusted close。
4. **F3 连续窗口未按市场日历验证**：先验证连续 20 日父缓坡，再处理 120 日低位回看。
5. **F4 底部/平台窗口未按市场日历验证**：平台父事件不可证明时不伪造 parent；父事件已证明但底部回看缺失时 warmup censor。
6. **F2 平台窗口未按市场日历验证**：平台父事件不可证明时不伪造 parent；day1..5 缺失仍显式 selection censor。
7. **F3 零成交误作 selection censor**：保留 facts-complete parent，作为流动性诊断并归入 not_selected。
8. **F1 缺连续跌停与无法卖出占比**：补充连续一字跌停日数、最大连续日数、unreachable/pending 事件数及明确分母比例。

## 二次复核 findings 与处置

- **重叠边界 off-by-one**：阻断上限由 `landmark+20` 改为 `landmark+19`，并锁定 day19 blocked/day20 allowed 契约测试。
- **缺棒会制造普通日 parent/censor**：冻结设计精化为“先证明父事件，再允许 selection/warmup censor”；父事件本身不可判定时不得进入 parent 分母。修订已先补记到 [Issue #38 评论](https://github.com/wf2311/fm-workbench/issues/38#issuecomment-5455139057)。
- **F2 零 prior mean volume 静默丢父事件**：保留平台+突破 parent，`volume_ratio=null`、`volume_expanded=false`，完整 day5 后归 not_selected，同时避免 breakout volume 为零时除零。

## 额外父池修正

F2 的放量条件属于 selection，不属于平台突破 parent。低于 1.50 倍、或 prior mean volume 为零的 facts-complete 平台突破均保留到 `not_selected`，避免 parent/qualified/not_selected 计数漏项。
