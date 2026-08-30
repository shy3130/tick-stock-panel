# Issue #48 可行性

## 结论

S1-S10 均已具备确定性研究实现，但仍是“可计算、可审计”，不是“已通过样本外验证”：

- S1/S8/S9 继续只消费显式传入的原始日线 bars/calendar。
- S2-S7/S10 使用 provider 窄扩展创建 catalog-pinned composite reader；每个交易日只通过 catalog 解析一次 minutes/trans，固定 generation manifest 身份后批量读取，不降级 raw。
- 240 根分钟桶只提供时间网格和封存全日成交量；盘中 high/low/amount/VWAP 均由逐笔重建。午盘/收盘边界允许少量逐桶归属差异，但封存的分钟与逐笔全日成交量必须严格相等，否则整日删失。
- S10 只使用 `daily_markets` exact-date `ltgb` 与 exact partition 的 manifest `source_version` 作为 `available_at`；不回退 `base_infos` 当前快照。股本事实无法证明在信号分钟前可得时，仅 S10 返回 `censor_pit_fact_missing`，其它盘中信号仍可评估。
- 任一日期的 route、240 桶、逐笔时间映射或全日成交量完整性失败时，该 symbol/day 进入显式删失，不使用日线 high/low 近似盘中路径。

### 冻结的盘中口径

- S2：14:30 至收盘跌幅至少 2%，尾段单位时间路径速度至少为此前绝对路径速度的 3 倍，且收盘位于全日区间底部 20%。
- S3：盘中触及 published limit-up 且收盘未封住；记录首次触板分钟与开板次数。
- S4：全日成交量至少为前 5 个完整交易日均量的 2 倍，`abs(close/pre_close-1) <= 1%`，且盘中最高涨幅至少 3%。
- S5：触及 published limit-down，分为封死、翘板仍在昨收下方、翘板翻红三支；封死分支明确标记同日不可达。
- S6：高开至少 2% 后，连续 5 根分钟收盘低于截至该分钟的逐笔累计 VWAP。
- S7：10:00-10:30 未突破开盘前 30 分钟高点，且 10:30 收盘低于累计 VWAP。
- S10：价格高于昨收，且截至同一分钟的累计换手至少为前 5 个完整交易日同时点均值的 2 倍；六日 `ltgb.available_at` 均不得晚于各自信号分钟。

所有盘中 evidence 都保存 ISO `available_at`、同日 `execution_session`、信号分钟 `execution_price`、`execution_reachable` 与 `existing_position_required=true`。聚合器对可达的盘中信号按同日信号价计算，对 S2/S4 这类收盘确认信号以下一交易日开盘计算；不产生订单或自动执行动作。

### 剩余研究门禁

当前没有冻结的真实 OOS 出场基线，因此任何样本内统计仍不得 promoted；S10 在早期历史通常因缺少可证明的股本写入时点而删失。后续必须先冻结独立 OOS 对照、coverage 窗口和分层规则，再决定 accepted/rejected。

## 风险与边界

MACD 采用固定 EMA12/EMA26/DEA9 与标准红柱 `2*(DIF-DEA)`，初始种子和有效暖机点冻结。S1 当前红柱峰值只取截至信号日的 running max，避免把未来红柱峰值带入过去判断。窗口不足使用明确 censor；S9 缺少昨收使用 `censor_pit_fact_missing`。研究基线除可由 bars 定义的 buy_hold 外，只接受显式预计算值，缺失不伪造。
