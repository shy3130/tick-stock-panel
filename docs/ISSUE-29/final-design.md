# ISSUE-29 final-design：左一K线防守位

> 状态：**已批准**。最终 reviewer approve，无 blocker/major；本文件是 [plan-v3.md](plan-v3.md) 的权威收敛副本；文档导航：[README.md](README.md) · [feasibility.md](feasibility.md) · [review-v1.md](review-v1.md) · [plan-v1.md](plan-v1.md) · [review-v2.md](review-v2.md) · [plan-v2.md](plan-v2.md) · [review-v3.md](review-v3.md)。
> 基线 `7bf2982`；本波仅文档，不改代码、不跑测试。

## 权威定义

`definition_version=v3`。v1 仅日线、单一 generation-pinned sealed canonical；固定参数只有：中位线 window=3、同高取最新、strict 包含（候选 A 被 B 包含 iff `A.high<=B.high && A.low>=B.low`，等点算包含）、破位后收回不撤销、ATR 吊灯 k=3。参数不在请求中开放，变体必须另立 RunCard。

研究价格使用同一 canonical generation 的前复权 OHLC（形态、防守线、归一化经济收益）；raw OHLC 仅作为报价/可卖性 evidence，字段必须区分 `research_*_adj` 与 `quote_*_raw`，adjusted open 不称实际报价或成交价。禁用 `turnover_rate`，禁止读写 `data/`、跨 generation 或跟随 current。

## 入场、状态机与 horizon

`uptrend(T) = close_adj(T)>MA60_adj(T) && MA20_adj(T)>=MA60_adj(T)`。唯一入场是 false→true 的 T 收盘 transition；T+1 是 pinned calendar 精确下一个 market day，且计为 day 1。`entry_id=normalized_symbol + signal_date(T)`，持仓或 pending_exit 内不重复入场；六臂始终按 entry_id 共用同一入场集。

中位线向左最多 10 根取第一根未被完全包含的 K 线，防守位为其 low_adj；close_adj 严格小于防守线才破位，下一可卖日处理。持仓中上涨状态丢失进入 `uptrend_lost`：保留最后线并停止上移，直到 close 创入场后新高且状态恢复才重算/上移；保留线继续检测破位。

共同观察 horizon 为 60 个 market days，day 1=T+1 执行日，终点为第 60 个 market day close。请求 end 必须覆盖终点，否则 entry 为 `CENSOR_HORIZON_INCOMPLETE`。六臂：buy-and-hold、ATR 吊灯、MA20、MA60、左一防守、左一+ATR（取较高线）。

## PIT markets pin 与买卖可达性

canonical manifest 的 `source_generations['markets']` 是唯一 immutable markets pin；按该 generation 解析 markets manifest/DB，校验 generation、hash、日期和标的覆盖。任何缺失/不一致整单 `UNAVAILABLE_MARKETS_PIN` 或 `UNAVAILABLE_MARKETS_MANIFEST_MISMATCH`；绝不跟随 markets current，也不使用 `current_path("markets")`。

同一 PIT bands/markets source 提供 raw OHLC、upper/lower、停牌、`signal_limit_up`/`signal_limit_down`。T+1 买入命中停牌、缺 bar、invalid raw_open、一字涨停分别为 `CENSOR_SUSPENDED`、`CENSOR_MISSING_BAR`、`CENSOR_INVALID_OPEN`、`CENSOR_BUY_LIMIT_UP`，不延后找价；卖出一字跌停由限跌停 signal+raw 同价精确构造 sellable=false。缺 markets 事实整单 unavailable。

## 统计、删失与响应契约

仅 `entry_executed` 且在共同 horizon 内取得 `terminal_exit` 或 `horizon_close` 的 complete segment 进入 net_return、MAE、MFE、holding_days；pending、horizon incomplete、未执行 entry 只进入 censored 和 denominator audit，不进入效果统计。不输出组合 NAV、年化收益、Sharpe、组合 MaxDD。

持有期净收益为成本后 adjusted-price normalized segment return。卖飞率固定 N={5,10,20}：离场后完整 N 个 market days 的 `max(close_adj)` 严格大于截至离场日（含离场日）的持仓 `peak_close_adj` 才计卖飞；窗口不足为 `CENSOR_DIAGNOSTIC_HORIZON_INCOMPLETE`。破位后跌幅固定 M=5，完整窗口公式 `min(close_adj_after_exit_5)/exit_research_value_adj - 1`；端点、分母和删失全部入 provenance。

请求 Pydantic `extra="forbid"`：`symbols:list[str]`（规范化去重、最多500）、`start:date`、`end:date`、`oos_start:date`（`start<oos_start<=end`）、`cost_bps:float`（默认10，0..1000）。未知字段、非法日期/symbol/cost→400；合法请求但 reader/required facts 缺失→200 + `status=unavailable`。

响应是按 `status` discriminator 的 Pydantic/TypedDict 联合。封闭枚举为：`ArmEnum = Literal["buy_hold","atr_chandelier_k3","ma20_hold","ma60_hold","zuoyi_defense","zuoyi_atr_combo"]`；`CensorCode = Literal["CENSOR_WARMUP_INSUFFICIENT","CENSOR_MISSING_BAR","CENSOR_INVALID_OPEN","CENSOR_SUSPENDED","CENSOR_BUY_LIMIT_UP","CENSOR_HORIZON_INCOMPLETE","CENSOR_PENDING_EXIT","CENSOR_DIAGNOSTIC_HORIZON_INCOMPLETE"]`；`WholeOrderUnavailableCode = Literal["UNAVAILABLE_READER","UNAVAILABLE_CANONICAL_PIN","UNAVAILABLE_MARKETS_PIN","UNAVAILABLE_MARKETS_MANIFEST_MISMATCH","UNAVAILABLE_REQUIRED_COLUMN","UNAVAILABLE_INVALID_PROVENANCE"]`。`CensorEvent={code:CensorCode; symbol:str; entry_id:str|null; signal_date:date|null; arm:ArmEnum|null; detail:str}`；`Segment={entry_id:str; status:Literal["complete","censored"]; exit_kind:Literal["terminal_exit","horizon_close"]|null; censor_code:CensorCode|null; net_return:float|null; mae:float|null; mfe:float|null; holding_days:int|null; exit_research_value_adj:float|null; exit_quote_open_raw:float|null; evidence:dict[str,object]}`；`ArmSegmentResult={arm:ArmEnum; segments:list[Segment]}`。`SummaryStat={metric:Literal["net_return","mae","mfe","holding_days"]; eligible_count:int; censored_count:int; mean:float|null; median:float|null; p05:float|null; p95:float|null; confidence_interval:ConfidenceInterval|null}`；`ConfidenceInterval={level:Literal[0.90,0.95]; low:float; high:float; method:Literal["bootstrap_percentile","wilson_score"]}`；`Diagnostics={sell_flee:list[SellFleeStat]（恰好 n=5/10/20 三项）; breakdown_depth:BreakdownDepthStat（m=5）}`；`Provenance` 必含 canonical/markets generation 与 manifest hash、required_columns、market_days、固定 parameters、cost_bps、horizon_days=60、diagnostics_horizons；`Verdict={value:Literal["accepted","rejected","unavailable"]; oos_complete_segments:int; minimum_required:int; rule:str}`。`UnavailableResponse` 固定为 `{status:Literal["unavailable"]; definition_version:Literal["v3"]; code:WholeOrderUnavailableCode; reasons:list[str]; request:ZuoyiRequestEcho; entry_ids:list[str]=[]; events:list[EntryEvent]=[]; arms:list[ArmSegmentResult]=[]; censored:list[CensorEvent]=[]; denominator_audit:list[DenominatorAudit]=[]; is/oos:StatsBlock(empty); diagnostics:Diagnostics(empty); verdict:Verdict(value="unavailable",...); provenance:Provenance|null}`。`ZuoyiEvaluateOk` 的 `status="ok"` 时 arms 必须恰好六项且覆盖 ArmEnum，events/segments 按 entry_id 对齐；`Segment.censor_code` 为 null 当且仅当 complete。`ZuoyiEvaluateOut=Annotated[ZuoyiEvaluateOk|UnavailableResponse,Field(discriminator="status")]`；unavailable 时上述列表固定为空、统计 count=0/stats=[]、diagnostics 列表空/rate=null，事件级 CensorCode 不得替代整单 code。
两种响应均显式包含顶层 `segments:list[Segment]`：`status="ok"` 时与 entry_id 对齐，`status="unavailable"` 时固定为空列表；因此 `arms/events/segments/censored` 的空结构与六臂完整结构均可直接由 Pydantic discriminator 校验。
EntryEvent 不变式同样固定：`status="entry_executed"` iff `censor_code=null`；
`status="censored"` 必须 `censor_code!=null`，并存在恰好一个同 `entry_id`、`arm=null`、
`code` 相同的 CensorEvent；CensorEvent.detail 必须非空，禁止空原因。

## 实施与门禁

verdict 只由 entry_id 对齐的 OOS complete segments 决定；样本不足或数据不可用为 unavailable，无稳定增量为 rejected。provenance 必含 canonical/markets generation 与 manifest hash、required columns、market days、参数、成本、horizon=60、diagnostics horizons。测试必须覆盖 pin 漂移/缺失、四类 T+1 买入 censor、分母、严格卖飞、M=5、uptrend_lost、截断不变性和六臂对齐。方案文档已通过最终复审；后续实施仍须完成这些验证，不写收益结果、不接策略池/监控/真实交易。
