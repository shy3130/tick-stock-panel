# Issue #50 可行性核实

## 结论

可引入为只读、默认不晋级的 OOS 候选池排除研究，但只能开放当前可由 pinned 数据证明的 V2/V4/V5。V1 原视频定义截断，固定为 `unavailable_definition_unverified`；V3 缺少带披露时间戳的 PIT 立案公告源，固定为 `unavailable_no_pit_announcement_source`，两者不允许代理。

生产编排绑定 canonical、daily market facts 与 presence universe 三份 pinned identity，在 OOS 日期逐 symbol-day 构造未过滤池和排除后池；信号在当日收盘可见，forward 从下一市场日开盘起算。输出通过研究 API 暴露，但 `promoted=false`，不进入 short pool、监控或交易。
