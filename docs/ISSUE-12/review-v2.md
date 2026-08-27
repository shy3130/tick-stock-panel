# 方案 v2 Review

二审发现并要求补齐三项：

1. manifest 必须冻结所有实际消费的逐笔/盘口/PIT 输入及 generation、校验和、覆盖范围；
2. 触板/炸板/回封按变体要求可排序逐笔，封板还要求历史盘口证据；
3. PIT 记录同时满足 `effective_at <= signal_time` 与 `available_at <= signal_time`。

以上均已写入当前 plan-v2。由于这些生产能力不存在，最终实现只能交付显式 unavailable 契约，不能宣称事件或 OOS 完成。
