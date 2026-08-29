# Issue #48 可行性

## 结论

S1、S8、S9 可以在现有 `Bar` 日线契约上做确定性检测。检测器只消费显式传入的内存 bars/calendar，不访问网络、文件或用户数据，也不产生订单、卖出或执行事件。

- S1：截至收盘判断窗口新高与 MACD 红柱相邻峰值递降。
- S8：三根连续 `close < open` 的阴线；十字星打断序列。
- S9：当日开盘相对上一交易日收盘低开至少 5%，信号只对既有持仓有意义。

S2-S7、S10 依赖不可替代的分钟不可变历史。本模块只声明 `unavailable_insufficient_immutable_history`，不使用日线 high/low 推断分钟路径。

## 风险与边界

MACD 采用固定 EMA12/EMA26/DEA9 与标准红柱 `2*(DIF-DEA)`，初始种子和有效暖机点冻结。S1 当前红柱峰值只取截至信号日的 running max，避免把未来红柱峰值带入过去判断。窗口不足使用明确 censor；S9 缺少昨收使用 `censor_pit_fact_missing`。研究基线除可由 bars 定义的 buy_hold 外，只接受显式预计算值，缺失不伪造。
