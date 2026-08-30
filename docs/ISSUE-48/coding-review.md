# Issue #48 编码复核

- S1-S10 均返回 shared `Detection`/`DetectionEvidence`；日线与盘中代码级 capability 已分离于请求时 runtime coverage/censor，不再把已实现检测器误报为 reader 未接入。
- composite reader 仅通过 active provider 的窄扩展创建；按日解析 catalog、固定 generation manifest、按日批量查询全部 symbols，拒绝 raw fallback。240 桶、逐笔时间映射和全日成交量守恒任一失败都会删失该 symbol/day。
- 真实冒烟发现 catalog 历史请求会被 later preliminary 抢占：2025-08-28 曾错误路由到 `tdx-trans-2026-08.duckdb`。已把精确日期范围的 `pinned_immutable` 置于 preliminary 回退之前，并增加包含无界 preliminary 行的回归测试。
- S2-S7/S10 的阈值、同日/次日执行 session、触板开板次数、跌停首次可成交翘板分钟、连续 5 分钟 VWAP 跌破和前 5 日同时点换手均有边界测试。S10 有 PIT 事实但不触发时现在产生 `qualified=false` evidence；只有事实缺失或 `available_at` 晚于信号分钟才 censor。
- S10 不使用 `base_infos` 当前股本；exact-date `ltgb` 必须连同 exact partition 的 manifest `source_version` 可证明。早期历史因此会保守删失 S10，不影响 S2-S7。
- 第一轮独立 review 无 P0，报告 1 个 P1：同日分钟 `execution_price` 是原始价，却直接与前复权 forward close 比较。已按信号日 `research_close_adj / quote_close_raw` 把执行价换到同一价格空间，并以复权因子 0.5 的回归用例锁定 10% 收益。第二轮独立复核结论为“闭环”，置信度 0.92。
- API/production 只输出研究统计、provenance、coverage 与 censor，不产生方向字段、订单或交易写入；无冻结 OOS 出场基线时仍不得 promoted。
