# 最终方案

冻结规则如下：放量 E 的 raw volume percentile >= P90、amount percentile >= P90，参考严格早于 E 的 20 个有效市场日；缺任一/非正值删失。整理自 E+1，连续 3 个完整市场日后冻结；冻结条件为每个整理日 raw_high-low 箱体宽度 <= 12% 且收盘落在箱体内，首个满足日 freeze_date，之后不重置；若整理期间 raw_close 突破箱体或窗口超过 15 日而未冻结，则事件失败，不重开。箱体只用 freeze 前整理 bar，突破柱排除。up/down 分别以 T 收盘严格越过 box_high/box_low 确认；评价从 T+1 下一可交易 bar，固定 horizon 1/5/10/20 个交易日，缺失删失。

PIT universe 按事件日 E 选择覆盖 E 的最新快照：`effective_from <= E <= effective_to` 且 `available_at <= E`；无唯一快照删失并记录逐事件 hash。交易日历使用版本化交易所 calendar；标的 status 另由 PIT listing/trading records 给出，市场开市缺 bar 与停牌/未上市分别计数，不能从 bars 推导。标签区间为闭区间 `[T+1,T+horizon]`；每折 train `[start,train_end]`、OOS `[oos_start,oos_end]`，purge 删除同 symbol 且区间相交事件，embargo 为 OOS 起点前最大 horizon 个市场日。区间接触视为重叠，按日期排序做传递闭包 cluster；仅保留最早且 T+1 和目标 horizon 全可观测事件，否则优先保留后续完整事件并记录删失。所有 manifest/universe/参数/hash 进入 provenance。

## 生产化状态更新（2026-08-27）

本文冻结的事件状态机、forward/OOS、重叠控制和成本诊断已经实现。generation-pinned 日线与版本化 calendar 已可用；唯一剩余硬门禁是带 available_at 的 PIT universe。没有该工件时服务继续 unavailable，不从 bar 覆盖或当前 instruments 反推资格。