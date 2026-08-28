# ISSUE-29 plan-v3：左一K线防守位（待最终复审）

> [README](README.md) · [可行性](feasibility.md) · [review-v1](review-v1.md) · [plan-v1](plan-v1.md) · [review-v2](review-v2.md) · [plan-v2](plan-v2.md) · [final-design](final-design.md)
> 基线 `7bf2982`；本波仅文档，不改代码、不跑测试。

## 1. 固定定义与唯一入场

`definition_version=v3`，全量固定参数：中位线 `window=3`、同高取最新、strict 包含（`A.high<=B.high && A.low>=B.low`，等点算包含）、破位后收回不撤销、ATR 吊灯 `k=3`。参数不进入请求；变体须新建研究 run。

`uptrend(T) = close_adj(T)>MA60_adj(T) && MA20_adj(T)>=MA60_adj(T)`。唯一 entry 是 `uptrend(T-1)=false -> uptrend(T)=true` 的 T 收盘 transition。T+1 为 pinned market calendar 的精确下一个 market day，entry day 计 day 1；同 symbol 持仓或 pending_exit 中不重复入场。`entry_id=normalized_symbol + signal_date(T)`，六臂逐 entry_id 对齐。

## 2. 请求模型与 T+1 买入可达性

请求为 Pydantic `extra="forbid"`：

| 字段 | 类型与约束 |
|---|---|
| `symbols` | `list[str]`，非空；规范化交易所代码、去重，最多 500；非法/超限 → 400 |
| `start` | `date`，含端点；`start < oos_start` |
| `end` | `date`，含端点；`oos_start <= end`，且必须覆盖每个 entry 的第 60 个 market day，否则该 entry `CENSOR_HORIZON_INCOMPLETE` |
| `oos_start` | 必填 `date`；IS=`[start,oos_start)`，OOS=`[oos_start,end]` |
| `cost_bps` | `float`，默认 10，`0<=cost_bps<=1000` |

T+1 买入与退出共用同一 PIT bands/markets source：raw OHLC、upper/lower limit、停牌和缺 bar。T+1 `raw_open` 无效、停牌、缺 bar 或命中一字涨停（`signal_limit_up` + raw OHLC 同价）时，不延后找价，entry 事件分别记 `CENSOR_INVALID_OPEN`、`CENSOR_SUSPENDED`、`CENSOR_MISSING_BAR`、`CENSOR_BUY_LIMIT_UP`。缺 immutable markets facts 为整单 unavailable。

## 3. Canonical 与 immutable markets pin

研究 OHLC 使用单一 published canonical generation 的前复权 `open/high/low/close`；raw_* 只作报价证据。canonical manifest 的 `source_generations['markets']` 是唯一 immutable markets pin：必须按该 generation 解析对应 markets manifest/DB，并校验 generation、manifest hash、日期/标的覆盖一致。任何缺失、不一致或无法解析 → **整个 evaluate `UNAVAILABLE_MARKETS_PIN`**；绝不跟随 `markets current`，禁止使用会调用 `current_path("markets")` 的 reader 路径。

同一 PIT markets generation 提供 `signal_limit_up`/`signal_limit_down`、upper/lower bands 与停牌事实。卖出一字跌停由 limit-down signal 与 raw OHLC 同价精确构造 `sellable=false`，不是仅凭 OHLC 猜测；缺失时整单 unavailable。adjusted 账本用于形态、线和归一化经济收益，raw 账本仅记录 `quote_*_raw` execution evidence，绝不称 adjusted open 为实际报价/成交价。

## 4. 防守状态机

截至完成 bar T，窗口 3 取最高 `high_adj`，同高最新；向左最多 10 根找第一根未被完全包含的左一，防守位为其 `low_adj`。收盘 `close_adj < defense_line_adj` 才破位，等点不破位；下一可卖 market day 的 raw quote 记录跳空与可卖性，研究收益仍按 adjusted 账本归一化。入场日不得退出（T+1）。停牌/一字跌停进入 pending，首个可卖日执行；数据末端未执行则删失。

持仓中上涨状态由 true 变 false（`close_adj<=MA60_adj` 或 `MA20_adj<MA60_adj`）时进入 `uptrend_lost`：保留最后防守线并停止上移/重算，保留线继续检查破位；直到 close 创入场后新高且上涨状态恢复，才恢复重算与上移。已确认破位次日收回不撤销。

## 5. 六臂、共同 horizon 与分母

每个 entry 的六臂共享 T+1 入场与共同 60 个 market-day horizon，day 1=T+1 执行日，终点为第 60 个 market day close；`end` 未覆盖终点即该 entry `CENSOR_HORIZON_INCOMPLETE`。六臂为 buy-and-hold、ATR 吊灯（`max_high_adj-3*ATR14_adj`）、MA20、MA60、左一防守、左一+ATR（取较高线）。

只有同时满足 `entry_executed` 且在共同 horizon 内得到 `terminal_exit` 或 `horizon_close` 的 complete segment，进入 net_return、MAE、MFE、holding_days 统计。pending exit、horizon 不完整和未执行 entry 只进入 censored 与 denominator audit，不进入效果统计；不得静默删除。

## 6. 逐段统计与 diagnostics

不构造组合 NAV，不输出年化收益、Sharpe 或组合 MaxDD。逐 entry、逐臂输出成本后持有期净收益、MAE、MFE、holding_days、换手及防守位距离 ATR 分位，并给出每个指标的 eligible/censored 分母。

卖飞率对每个完整离场分别计算 N=`{5,10,20}`：离场后完整 N 个 market days 内 `max(close_adj_future_N)` **严格大于**持仓截至离场日（含离场日）的 `peak_close_adj_through_exit` 才计卖飞；窗口不足记 `CENSOR_DIAGNOSTIC_HORIZON_INCOMPLETE`，不进该 N 的分母。破位后下跌深度固定 M=5，必须有完整 5 个 market days，公式为 `min(close_adj_after_exit_5)/exit_research_value_adj - 1`；窗口不足同码删失。所有端点规则、N/M、成本、分母进入 provenance。

## 7. 固定 response schema 与枚举

以下定义可直接映射为 Pydantic model 或 `TypedDict`；`ZuoyiEvaluateOut` 是按 `status` discriminator 的联合类型。

```text
ArmEnum = Literal[
  "buy_hold", "atr_chandelier_k3", "ma20_hold", "ma60_hold",
  "zuoyi_defense", "zuoyi_atr_combo"
]
CensorCode = Literal[
  "CENSOR_WARMUP_INSUFFICIENT", "CENSOR_MISSING_BAR",
  "CENSOR_INVALID_OPEN", "CENSOR_SUSPENDED", "CENSOR_BUY_LIMIT_UP",
  "CENSOR_HORIZON_INCOMPLETE", "CENSOR_PENDING_EXIT",
  "CENSOR_DIAGNOSTIC_HORIZON_INCOMPLETE"
]
WholeOrderUnavailableCode = Literal[
  "UNAVAILABLE_READER", "UNAVAILABLE_CANONICAL_PIN",
  "UNAVAILABLE_MARKETS_PIN", "UNAVAILABLE_MARKETS_MANIFEST_MISMATCH",
  "UNAVAILABLE_REQUIRED_COLUMN", "UNAVAILABLE_INVALID_PROVENANCE"
]
ZuoyiRequestEcho = {
  symbols: list[str]; start: date; end: date; oos_start: date; cost_bps: float
}
EntryEvent = {
  entry_id: str; symbol: str; signal_date: date; entry_date: date | None
  status: Literal["entry_executed", "censored"]
  censor_code: CensorCode | None
  research_entry_value_adj: float | None
  quote_entry_open_raw: float | None
  evidence: dict[str, object]
}
CensorEvent = {
  code: CensorCode; symbol: str; entry_id: str | None
  signal_date: date | None; arm: ArmEnum | None; detail: str
}
Segment = {
  entry_id: str
  status: Literal["complete", "censored"]
  exit_kind: Literal["terminal_exit", "horizon_close"] | None
  censor_code: CensorCode | None
  net_return: float | None; mae: float | None; mfe: float | None
  holding_days: int | None
  exit_research_value_adj: float | None
  exit_quote_open_raw: float | None
  evidence: dict[str, object]
}
ArmSegmentResult = { arm: ArmEnum; segments: list[Segment] }
ConfidenceInterval = {
  level: Literal[0.90, 0.95]; low: float; high: float
  method: Literal["bootstrap_percentile", "wilson_score"]
}
SummaryStat = {
  metric: Literal["net_return", "mae", "mfe", "holding_days"]
  eligible_count: int; censored_count: int
  mean: float | None; median: float | None
  p05: float | None; p95: float | None
  confidence_interval: ConfidenceInterval | None
}
StatsBlock = {
  segment_count: int; complete_count: int; stats: list[SummaryStat]
}
SellFleeStat = {
  n: Literal[5, 10, 20]; sold_flee_count: int; eligible_exits: int
  censored_windows: int; rate: float | None
}
BreakdownDepthStat = {
  m: Literal[5]; eligible_exits: int; censored_windows: int
  mean_depth: float | None; median_depth: float | None
}
Diagnostics = {
  sell_flee: list[SellFleeStat]; breakdown_depth: BreakdownDepthStat
}
DenominatorAudit = {
  metric: Literal[
    "net_return", "mae", "mfe", "holding_days",
    "sell_flee_n5", "sell_flee_n10", "sell_flee_n20",
    "breakdown_depth_m5"
  ]
  eligible_count: int; censored_count: int; excluded_count: int
  codes: dict[str, int]
}
Verdict = {
  value: Literal["accepted", "rejected", "unavailable"]
  oos_complete_segments: int; minimum_required: int; rule: str
}
Provenance = {
  definition_version: Literal["v3"]
  canonical_generation: str; canonical_manifest_sha256: str
  markets_generation: str; markets_manifest_sha256: str
  required_columns: list[str]; market_days: int
  window: Literal[3]; tie_break: Literal["latest"]
  inclusion: Literal["strict"]; recovery: Literal["no_cancel"]
  atr_k: Literal[3]; cost_bps: float; horizon_days: Literal[60]
  diagnostics_horizons: { sell_flee: list[Literal[5,10,20]]; breakdown_depth: Literal[5] }
}
ZuoyiEvaluateOk = {
  status: Literal["ok"]; definition_version: Literal["v3"]
  request: ZuoyiRequestEcho
  entry_ids: list[str]; events: list[EntryEvent]; segments: list[Segment]
  arms: list[ArmSegmentResult]; censored: list[CensorEvent]
  denominator_audit: list[DenominatorAudit]
  is: StatsBlock; oos: StatsBlock
  diagnostics: Diagnostics; verdict: Verdict; provenance: Provenance
UnavailableResponse = {
  status: Literal["unavailable"]; definition_version: Literal["v3"]
  code: WholeOrderUnavailableCode; reasons: list[str]
  request: ZuoyiRequestEcho
  entry_ids: list[str] = []; events: list[EntryEvent] = []
  segments: list[Segment] = []; arms: list[ArmSegmentResult] = []
  censored: list[CensorEvent] = []
  denominator_audit: list[DenominatorAudit] = []
  is: StatsBlock(empty); oos: StatsBlock(empty); diagnostics: Diagnostics(empty)
  verdict: Verdict(value="unavailable", oos_complete_segments=0,
                   minimum_required=0, rule="unavailable")
  provenance: Provenance | None
}
ZuoyiEvaluateOut =
  Annotated[ZuoyiEvaluateOk | UnavailableResponse, Field(discriminator="status")]
```

固定不变式：`status="ok"` 时 `arms` 恰好 6 项且覆盖 `ArmEnum` 全部值，所有臂按
`entry_id` 对齐，顶层 `segments` 与 entry_id 对齐，`verdict.value` 只能是
`accepted` 或 `rejected`；`status="unavailable"` 时
`entry_ids/events/segments/arms/censored/denominator_audit` 必须是空列表，`is/oos` 是
`segment_count=complete_count=0、stats=[]` 的空结构，`diagnostics` 的列表为空且
所有计数为 0、rate 为 null，`verdict.value="unavailable"`。`Segment.censor_code`
为 null 当且仅当 `status="complete"`；事件级 `CensorCode` 不得替代整单
`WholeOrderUnavailableCode`。

`EntryEvent.status="entry_executed"` 当且仅当 `censor_code=null`；`status="censored"` 必须
`censor_code!=null`，且必须存在恰好一个相同 `entry_id`、`arm=null`、`code` 相同的
`CensorEvent`。`detail` 必须为非空字符串；禁止空原因或无对应证据的事件 censor。

请求校验错误（日期、symbols、未知字段、成本）→ HTTP 400；合法请求但 immutable
reader/required facts 缺失 → HTTP 200，`status="unavailable"`、整单 code/reasons。

## 8. Verdict、测试与映射

verdict 只由 entry_id 对齐的 OOS complete segments 决定；无真实结果不预填 accepted。测试须覆盖 markets pin 漂移/缺失、T+1 四类买入 censor、完整/不完整 horizon、逐项分母、卖飞严格大于、M=5、uptrend_lost、截断不变性及六臂对齐。

R1（markets pin）→§3；R2（买入可达）→§2；R3（分母）→§5/§6；R4（卖飞公式）→§6；R5/R7/R9（已 resolved）→§1/§3/§4；R6（horizon）→§2/§5；R8（schema）→§2/§7。