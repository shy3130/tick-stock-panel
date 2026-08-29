# Issue #47 可行性

## 结论

日频版本可行。现有 sealed canonical 日线提供复权研究 OHLCV 与原始报价，pinned markets generation 提供 `published_limit_up`、`pre_close`、历史制度、ST 状态，足以在信号时点重建 F1–F4。实现只做研究，不进入默认短线池。

## 冻结边界

- F1：只读 PIT 涨停价；缺失时仅用同日 PIT `is_st/regime + pre_close` 回退，不能用当前名称或当前板块制度。
- F2：向上跳空后第三个完整交易日才确认，`signal_date=available_date=t+3`，不回填缺口日。
- F3：标的连续上涨收盘必须强于显式基准；基准缺失即删失。
- F4：连续 5 日量能均高于此前 20 日基线；暖机不足即删失。
- 组合：四项都可评估后，至少三项命中；单项与组合保持独立分母和 verdict。

## 不可接受的降级

不得把“主力/控盘”当事实，不得用 future-surge 标签参与 detector，不得忽略 PIT 事实缺口，也不得用必要条件命中率冒充预测能力。
