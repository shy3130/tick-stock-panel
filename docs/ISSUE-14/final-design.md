# 最终方案

冻结规则如下：放量 E 的 raw volume percentile >= P90、amount percentile >= P90，参考严格早于 E 的 20 个有效市场日；缺任一/非正值删失。整理自 E+1，连续 3 个完整市场日后冻结；冻结条件为每个整理日 raw_high-low 箱体宽度 <= 12% 且收盘落在箱体内，首个满足日 freeze_date，之后不重置；若整理期间 raw_close 突破箱体或窗口超过 15 日而未冻结，则事件失败，不重开。箱体只用 freeze 前整理 bar，突破柱排除。up/down 分别以 T 收盘严格越过 box_high/box_low 确认；评价从 T+1 下一可交易 bar，固定 horizon 1/5/10/20 个交易日，缺失删失。

PIT universe 按事件日 E 选择覆盖 E 的最新快照：`effective_from <= E <= effective_to` 且 `available_at <= E`；无唯一快照删失并记录逐事件 hash。collection 的 next/previous market day 由同一 exact pinned fstore generation 的 `trade_date(tdate,isopen,mkt,lastdate,nextdate)`（`isopen=3`）解析，calendar identity 绑定 source generation 与 manifest SHA-256；volume-breakout 历史扫描仍使用独立版本化交易所 calendar seam。标的 status 另由 PIT listing/trading records 给出，市场开市缺 bar 与停牌/未上市分别计数，不能从 bars 推导。标签区间为闭区间 `[T+1,T+horizon]`；每折 train `[start,train_end]`、OOS `[oos_start,oos_end]`，purge 删除同 symbol 且区间相交事件，embargo 为 OOS 起点前最大 horizon 个市场日。区间接触视为重叠，按日期排序做传递闭包 cluster；仅保留最早且 T+1 和目标 horizon 全可观测事件，否则优先保留后续完整事件并记录删失。所有 manifest/universe/参数/hash 进入 provenance。

## 生产化状态更新（2026-08-27）

事件状态机、forward/OOS、成本诊断、重叠控制与 PIT universe SCD 已实现。SCD collector 固定 exact published fstore generation，eligible v1 仅保留 canonical A 股且 `ssdate <= collection_date`，并以 collection 后下一交易日作为 `effective_from`；reader 通过 event-date identity 返回冻结集合，完整性错误使整次研究 unavailable。daily pipeline 复用唯一盘后 hook 执行 best-effort publish，失败不切换 current。

首个真实 snapshot `20260827T153316Z-20c87c09f41cffb9` 已发布，固定 fstore source generation `20260827T134914` 与 manifest SHA-256，`effective_from=2026-08-28`，eligible symbols=5903。reader smoke 证明 collection day `2026-08-27` unavailable、下一交易日起按 event-date identity 可读。定向 27 项测试、静态错误检查和独立二次 Review 均通过；首个 effective_from 之前的历史日期继续 unavailable，禁止从 bars、当前 instruments 或任何历史表回填。