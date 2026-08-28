# ISSUE-29 review-v2：对 plan-v2 的独立复审记录

> 审查对象：[plan-v2.md](plan-v2.md)（基线 `7bf2982`）。
> 审查来源：独立 coding review（ReviewZuoyiPlan2），本文件忠实转写其全部 finding 与结论，未做辩护或改写。
> 日期：2026-08-28 · 对应 Issue：[wf2311/fm-workbench/issues/29](https://github.com/wf2311/fm-workbench/issues/29)

## 总体结论

**verdict：`incorrect`（最终门禁：reject，尚有 P1 blocker，不能进入实现）。** confidence：0.98。

R1、R2、R3、R6 为 partial；R4、R8 为 unresolved；R5、R7、R9 已 resolved。R 编号沿用 [review-v1.md](review-v1.md) 的 finding 序号。

## Findings

P1 = blocker 级；P2 = 必须修复级。共 6 项：3×P1、3×P2。

### R1（P1，confidence 0.99）固定与 canonical generation 绑定的限跌停事实源

plan-v2 §3.2 要求限跌停 signal 来自"同 generation"的 markets reader，但现有 `PublishedCanonicalDailyReader` 只固定 canonical manifest；现有 `PublishedDailyMarketFactsReader.from_repository()` 反而通过 `current_path("markets")` 取得调用时的当前 markets generation，且 `PublishedNShapeResearchReader` 也只能并列记录两个不同 generation。canonical manifest 已记录构建时的 `source_generations["markets"]`，但方案未规定按该值解析 immutable markets 文件、校验其 manifest，或在无法解析时 unavailable。因此按现有接口实现会在 canonical 与当前 markets 已发生切换时混入错误的限价/ST 事实，直接改变一字跌停的 pending_exit 和收益样本，R1 仍未解决。

### R2（P1，confidence 0.98）冻结 T+1 入场的可买性判定和 PIT 输入

方案把 T+1 "不可买"列为 entry censor 条件，却没有定义其精确规则或要求 `signal_limit_up`/限涨停事实。现有引擎的 `_can_buy` 会分别拒绝停牌、无效 open 和由 `signal_limit_up` 加 raw OHLC 同价构成的一字涨停；plan-v2 §3.2 只为卖出定义了限跌停 signal 与 `sellable=false`。这会让实现者对一字涨停或停牌的 T+1 各自选择纳入、延后或删失，导致六臂 common entry set 不可复现；应为买入补齐同一 immutable markets source、raw OHLC 条件、censor code 和缺失时 fail-closed 规则。R2 仅部分解决。

### R3（P1，confidence 0.97）预先规定右删失段进入各统计量的规则

plan-v2 §5 仅要求对 horizon_incomplete/pending_exit_censored 标记，并称"其是否进入某项统计的分母在 payload 中单列"，却没有规定持有期净收益、MAE、MFE、持有天数及各臂汇总究竟纳入完整 horizon、已成交退出、未成交 pending 与不完整 horizon 的哪些段。不同实现可合理地排除或把已观察的部分段纳入，得到不同 OOS 分母和统计摘要；将结果写回 payload 不能消除这种实现时自由度。应在实施前为每个统计量冻结 eligible 条件、censor 表达和分母，R3 仍未完成。

### R4（P2，confidence 0.99）定义卖飞率的事件条件和计算公式

plan-v2 §6 固定了 N 的三个值及窗口端点，但没有定义"卖飞"何时成立：只定义离场前累计最大 close 和未来 N 日窗口，未给出未来最大值与 exit_research_value/持仓峰值之间的比较公式、阈值或分子。因此同一组 exit 可以按"未来高于离场价""重回持仓峰值"或任意涨幅阈值得出不同卖飞率；eligible/censored 分母也无法确定。R4 所需的可审计诊断仍不完整。

### R5（P2，confidence 0.96）定义 60 日 horizon 的起点和终点包含规则

共同 horizon 只写为"同一个 T+1 入场和 60 个交易日"，没有规定 T+1 入场日是否计作第 1 日、共同终点是第 60 个 market day 的 close 还是其后一个交易日，也没有将该终点与请求 `end` 的包含边界关联。两种实现会分别在入场后第 59 或第 60 个后续交易日结算，并对末端 entry 给出不同的完整/删失状态及收益。故 R6 的固定共同观察终点仍只部分完成。

### R6（P2，confidence 0.98）冻结 evaluate 响应对象和 censor code 的具体 schema

plan-v2 §7 只枚举响应顶层概念，未定义 `entry_ids`、六臂逐段结果、`censored`、IS/OOS 分母、统计摘要、diagnostics、verdict 和 provenance 的字段类型、嵌套关系、可选性或合法枚举；此前各处定义的 censor 情形也没有对应的固定 code 集。因而无法据此建立可验证的 Pydantic `response_model`，且客户端无法区分例如 T+1 不可买、horizon 不完整、pending exit、diagnostic 不完整与整单 unavailable。R8 要求的完整请求/响应契约仍未满足。

## 与本次修订的关系

6 项已逐条落入 [plan-v3.md](plan-v3.md)；R1–R9 与 v3 章节的映射及 resolved 状态见 plan-v3 §10。plan-v2 保留原样作为被拒存档，不回写。
