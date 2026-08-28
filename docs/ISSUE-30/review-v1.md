# ISSUE-30 review-v1：对 plan-v1 的独立评审

> 结论：**Reject（overall_correctness = incorrect，confidence 0.98）**。
> 审阅对象：[plan-v1.md](plan-v1.md)（`docs/ISSUE-30/plan-v1.md`，v1 冻结草案）· 关联：[Issue #30](https://github.com/wf2311/fm-workbench/issues/30) · [README](README.md)
> 修复记录：[plan-v2.md](plan-v2.md)（逐条 R1–R5 回应）

## 总体意见（忠实转写）

> 应拒绝当前 plan-v1：PIT 跌停事实、逐候选虚拟结局、精确 T+1 执行日和 OOS 分段统计均未由现有 reader/engine 契约支撑，实施会导致撮合放宽、延迟入场或 IS/OOS 统计混淆。最小修订清单：补齐/明确 PIT 下限重建与 fail-closed 条件；为引擎增加按候选返回的执行结局；在调用引擎前按 pinned 日历锁定 T+1；将每臂 IS/OOS stats/layers 和 verdict 数据源写入 API；删除或重新定义独立候选模式下的组合换手与最大回撤。

## Findings

### R1 补齐可派生的精确跌停事实（priority 1，confidence 0.98）

位置：`docs/ISSUE-30/plan-v1.md` 第 47–51 行。

> `PublishedDailyMarketFactsReader.limit_regime_facts()` 只返回 `limit_up_price`、`is_st` 和 `regime`，没有跌停价或停牌事实（`daily_market_research.py:160-185`）；而撮合器只有在 panel 的 `signal_limit_down` 为真时才会阻塞跌停卖出（`engine.py:852-859,930-960`）。方案要求将"精确 PIT 涨跌停事实"并入这两个 flag，却没有规定如何从现有输入生成下限 flag；按现状实现会只能填 `False` 或退化为无约束卖出，直接违背 fail-closed 并低估止损/退出阻塞。应在方案中要求 markets reader 提供 PIT 跌停事实，或明确以 PIT `regime`、历史 ST 状态、raw 前收和交易所舍入规则重建上下限；任一所需事实缺失即整单 unavailable。

实现者核实（本波复查基线 7bf2982）：属实。`limit_regime_facts` 返回键仅 `limit_up_price/name/is_st/regime`；板块规则见同文件 `_regime`（`daily_market_research.py:148-158`）。

### R2 扩展引擎以输出逐候选执行结局（priority 1，confidence 0.99）

位置：`docs/ISSUE-30/plan-v1.md` 第 104–110 行。

> 虚拟结局要求按 `(symbol, entry_signal_date)` 区分 none 臂的成交、涨停/停牌阻塞和缺未来 bar（第 104–110 行），但 `simulate_independent_candidates` 只在成交时追加 `TradeRecord`，对未成交候选仅在 `stats["execution"]` 返回各原因的聚合计数（`engine.py:1068-1071,1955-1978`）。因此服务无法把某个被过滤事件 join 到其 none 臂的具体阻塞原因，也无法判断它应为 `censored`；按聚合计数猜配会把虚拟收益或删失归到错误事件。方案应先增加/约定逐候选、带 signal date 的执行结局输出（成交 trade 或未成交 reason），再以该结构完成 one-to-one join，不能以现有 aggregate stats 作为 join 来源。

实现者核实：属实。`_can_buy` 失败路径为 `_count(block_reason); continue`（`engine.py:1068-1071`）；`_calc_independent_candidate_result` 仅把 `execution` 聚合 dict 放入 stats（`engine.py:1955-1978`）。

### R3 在调用撮合器前锁定 T+1 的实际执行日（priority 1，confidence 0.98）

位置：`docs/ISSUE-30/plan-v1.md` 第 53–60 行。

> 方案规定"下一交易日/执行 bar 缺失"必须事件级 censored（第 59 行），但独立候选路径的 `open_t+1` 只是把 mask 移到同一 symbol 的下一行（`engine.py:815-820`），并不以 pinned 市场日历验证该行就是 T 的下一交易日。若 canonical 对停牌日没有 OHLC 行，T 后首个可用 bar 会被当作入场日，造成停牌后的延迟入场而非删失，改变 T+1 执行语义和各臂统计。方案必须规定用 pinned calendar 找到 T 的精确下一市场日、检查该日有可执行 bar，并在构造 entries 前将缺 bar 事件 censored（或填入该日不可交易占位行使引擎明确阻塞），不得依赖当前相邻行 shift。

实现者核实：属实。`entry_fill=open_t+1` 的 shift 为 `ent[1:] = ent_raw[:-1] & same_prev_symbol`（`engine.py:815-820`）。

### R4 在响应契约中固定 IS/OOS 分段统计（priority 1，confidence 0.98）

位置：`docs/ISSUE-30/plan-v1.md` 第 112–118 行。

> verdict 被要求只基于 OOS、IS 仅披露（第 6–11 行），但固定 `arms.*` 只有单个 `stats` 和 `layers`，stats 白名单也没有 `is`/`oos` 容器（第 112–118、150–167 行）；事件虽有 `segment`，却没有规定统计按 signal date、实际入场日还是退出日归段。实现若直接使用 arm 的单一 stats 会把 IS 混入 verdict，或无法向调用方披露/复算 OOS 依据。应将每臂契约明确为按信号日分段的 `segments.is`、`segments.oos`（各自含同一 stats/layers 与 censored/blocked 口径），并要求 verdict 仅消费 `segments.oos.stats`；同时更新白名单和固定响应结构。

### R5 移除独立候选路径的组合统计承诺（priority 2，confidence 0.97）

位置：`docs/ISSUE-30/plan-v1.md` 第 95–102、112–118 行。

> 方案指定独立候选撮合却把 `max_drawdown`、`turnover` 和 `cost_total` 列为既有可复用 stats（第 95–102、112–118 行）。该路径只输出逐候选 trades 和 aggregate execution；其 `initial_capital` 不参与候选撮合，且 result 没有 turnover/cost 字段（`engine.py:1955-2074`）。现有 `turnover`/cost breakdown 仅在 portfolio 统计中按实际 equity curve 与 `initial_capital` 计算（`engine.py:2128-2149`），而独立路径的所谓 equity curve 明确是按退出日平均样本收益构造、不是账户净值（`engine.py:1986-1988`）。直接把这些值复用为组合换手/最大回撤会产生无定义的统计。应从该研究白名单移除它们，或另行冻结一个明确标记为"candidate sample"的公式与分母；不能把 portfolio 指标伪装成独立候选结果。

实现者核实：属实。样本曲线注释"不是账户净值"（`engine.py:1986-1988`）；`turnover` 与成本合计仅出现于组合统计（`engine.py:2143-2146`）。

## 评审后处置

- v1 全文保留为历史记录，不就地修改；所有修复在 [plan-v2.md](plan-v2.md) 逐条落位，未修订条款继续以 v1 为准。
- 复审（review-v2）须确认 R1–R5 修复后才允许进入实现波次。
