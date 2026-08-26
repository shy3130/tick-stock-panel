# 方案 v2（终审修订）

当前 `get_enriched_range` overlay 与 current universe 不能支撑可复现 OOS；实现前置为 generation-pinned canonical reader、PIT eligible-universe 快照（含 as_of/hash）和逐标的固定市场日历。缺失则只返回 unavailable/描述性结果，拒绝基线/OOS 效果结论。价格统一使用已有 `raw_high/raw_low/raw_close`，缺 raw 字段删失；不假设 raw_open。

状态机逐日推进：放量事件日 E 只用 E 之前严格 20 个有效交易日定义；整理从 E+1 开始，窗口 3–15 个市场日。每个状态固定记录 box_start、freeze_date、box_high/box_low；箱体边界在整理状态达到冻结条件的当日收盘确定，突破柱不得参与箱体计算，只有固定允许的重置条件才能重开状态。放量使用 raw volume 与 amount 各自固定分位阈值并要求同时满足；整理均量只取 E+1 至突破前一日的完整正值 bar，分别记录相对 E 与 E 前 20 日均量。

突破变体为 `up_breakout`（raw_close>冻结 box_high）和 `down_breakout`（raw_close<冻结 box_low），T 日收盘确认，forward/执行诊断最早从同标的 T+1 下一可交易 bar；停牌/涨跌停/缺 T+1 删失。每个结果固定 `[signal_date,label_end_date]`，walk-forward 每折删除与 OOS 相交的训练标签并以最大 horizon 做 embargo。相邻事件按 symbol+标签区间组成 cluster，每簇只保留最早完整事件；输出 raw_events、clusters、统计样本数。

所有输出包含 generation/manifest 字节 hash、universe hash、参数/代码版本、证据、覆盖、删失和降级；不把洗盘/出货当事实，不接 short_pool/Agent/交易。缺真实 reader 时 fail-closed。测试覆盖箱体冻结、突破柱排除、缺交易日、overlay 不可用、PIT universe 缺失、双量能门禁、上下突破、假突破、低流动性、重叠 cluster 和 T+1 删失。
