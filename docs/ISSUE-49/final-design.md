# Issue #49 最终设计

入口：`app.services.n_shape_pullback_depth.evaluate_n_shape_pullback_depth`；API 为 `POST /api/research/factors/n-shape-pullback-depth/evaluate`。

输出 envelope 含 `factor/request/provenance/coverage/research/events/censored/promoted`。`promoted=false`，不接入 short pool、监控或交易。

`research.populations` 独立报告 A/B/C、不分档全样本、C×金凤凰：

- 按请求窗口的市场日历做 60/20/20 train/validation/test，不按稀疏事件日期切分；
- 5/10/20 市场日收益从 T+1 open 起算并扣 20 bps round-trip cost；每个结果记录 `available_date`，只有在所属 split 结束前完全兑现才进入统计，否则计入 `censored_cross_split`；
- 同时报告胜率、再创新高率、结构失败率和 95% 区间；
- A/B/C 相对不分档，C×金凤凰相对 C；只在 validation 增量为正且 test 增量下界为正、test 至少 30 事件/10 标的时 `accepted`；
- 不分档队列只在 test 收益区间下界为正时 `accepted`；样本不足为 `unavailable_insufficient_samples`；
- test 另给确定性同分布 rotation placebo，A/B/C 统计禁止跨档平均。

敏感性包含分档边界整体 ±0.05 重分档计数，以及 fixed zigzag 5%/10% 事件数；均不生成第二套可择优 verdict。
