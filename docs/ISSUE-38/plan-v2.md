# ISSUE-38 方案 v2

> 本版替代 [plan-v1.md](plan-v1.md)，完整吸收 [review-v1.md](review-v1.md)。未在本版重述的 F1-F4 数值定义、模块文件布局、禁止项和测试矩阵保持 v1；最终设计将给出自包含契约。

## 1. 可达性证据修正

`MarketFact.suspended/buyable/sellable` **不是 required facts**。现有 `PublishedDailyMarketFactsReader` 不生成这三个派生值（`daily_market_research.py:362-442`）。

执行器只依赖同一 pinned markets row 的：

- `raw_open/raw_high/raw_low/raw_close/pre_close`；
- `published_limit_up/published_limit_down`；
- `regime/is_st/name`；
- row presence 与 market calendar。

派生规则：

- 缺整日 row 或任一 required raw/band/regime/is_st：整单 `unavailable_market_facts_incomplete`，因为同一 run 的制度/可达性不能局部猜测；
- `raw_open==raw_high==raw_low==raw_close==published_limit_up`：entry unreachable，一次 event censor；
- `raw_open==raw_high==raw_low==raw_close==published_limit_down`：exit unreachable，进入 pending exit；
- raw OHLC 全相等但不在 band：合法一价成交日，不视为停牌；
- canonical 有 market day 但 markets row 缺失：不可据此推断停牌，整单 unavailable；
- 所有 band 比较使用 `abs_tol=0.005` 与冻结的两位小数制度价格。

这避免用不可证明的 `suspended/buyable/sellable`，也不把缺 row 猜成停牌。

## 2. PIT universe 三态

请求开始时构造 `PublishedUniverseScdReader` 并固定 generation/manifest：

1. reader 未发布、generation/ledger/hash 不完整，或任一需要的 decision date 无 interval coverage：整单 `unavailable_universe_scd`；
2. interval 完整且 symbol 明确不在 `eligible_symbols(date)`：该父事件为 `pit_universe_ineligible`，进入 denominator audit，不计算收益；
3. symbol 在池但后续 canonical bar/horizon 不完整：event censor。

不得用 markets `universe(start,end)`、当前 instruments 或请求 symbols 自身替代日级 PIT membership。

## 3. selection landmark 与互斥分组

每个父事件冻结一个 **selection landmark**；`qualified` 与 `not_selected` 是互斥且并集等于 facts-complete parent events。两组都从 landmark 后的同一 market day raw open 建立同一 20 日观察时钟。

| 因子 | 父事件 | selection landmark | qualified | not_selected |
|---|---|---|---|---|
| F1 | 3+ 连板后首阴 | 首阴后第 1 个 market day 收盘 | 首阴守 MA5 且当日量能与首阴互补 | facts 完整但 MA5/量能任一未通过 |
| F2 | 20 日平台收盘突破 | 突破后第 5 个 market day 收盘 | 1..5 日内出现首个满足放量突破后的缩量守位回踩 | 完整观察 5 日仍未命中 |
| F3 | 低位缓坡新高父事件 | 信号当日收盘 | 阳线占比与缩量条件均通过 | 任一未通过 |
| F4 | 20 日平台收盘突破 | 突破当日收盘 | 同日满足底部、放量、大阳 | 任一未通过 |

F1 landmark bar 或 F2 完整 5 日 decision window 缺失时为 `censor_selection_window_incomplete`，不进入 qualified/not_selected selection 统计。F2 即使第 1 日已满足回踩，v1 研究也等到第 5 日 landmark 才开始同口径收益观察；“早确认即执行”属于后续独立策略研究，禁止从本结果外推。

selection verdict：

- parent pool 只做描述性统计；
- 检验对象固定为 landmark/horizon 对齐的互斥 `qualified` vs `not_selected`；
- 两侧 OOS complete segments 均至少 30，且按 symbol 聚类 bootstrap；
- 只有 OOS 成本后 20 日 terminal return 均值差的 95% cluster-bootstrap CI 下界 `>0` 才 accepted；下界 `<=0` 且两侧样本充足为 rejected；任一侧不足为 unavailable。

这里不是逐事件 paired 差值，因为 qualified/not_selected 是互斥事件；bootstrap 采样单位为 symbol，避免同标的重复事件伪增大样本量。

## 4. holding 共同二十日时钟

holding 仅在 qualified 事件上比较 `dynamic_defense` 与 `fixed_hold_20d`：

- 两臂共享 landmark 后的同一 entry quote 和第 20 个 market day 终点；
- dynamic arm 破位后在第一可卖日实现现金；从退出日起把扣费后的现金价值常数延续到共同终点；
- fixed arm 持有到共同终点；若终点不可卖，不需要假设卖出，终值使用 adjusted close 并明确为 mark-to-market；若合同要求已实现退出，则另报 realization censor，不改变共同路径诊断；
- terminal return、MAE/MFE 均在共同 20 日路径上计算；dynamic 退出后的现金路径不再受市场波动；实际 `holding_days`、pending days、exit quote 单列；
- holding verdict 使用同一 event_id 的 paired symbol-cluster bootstrap。

accepted 条件：OOS qualified complete events 至少 30；dynamic-fixed terminal-return 差 95% paired cluster-bootstrap CI 下界 `>=0`，且 MAE 差 CI 上界 `<0`。任一收益或 MAE 条件明确失败为 rejected；样本不足为 unavailable。

## 5. F4 诊断分母

F4 强弱层与假突破使用独立 `diagnostic_complete_5d` 分母：

- 突破后 1..5 日完整才分类；
- 窗口不足写 `censor_diagnostic_window_incomplete`，不归 `unclassified`；
- 分类优先级 `broken > very_strong > strong > unclassified`；
- 该层不进入 signal、selection、holding mask 或 verdict。

F2 假突破同样使用独立完整 5 日诊断分母；其 selection 5 日窗口与收益观察 20 日窗口是两个显式不同的时钟。

## 6. 响应不变式补充

每个 factor result 必须满足：

```text
facts_complete_parent_count
  = qualified_count + not_selected_count
parent_count
  = facts_complete_parent_count
    + pit_universe_ineligible_count
    + selection_window_censored_count
```

`qualified` 与 `not_selected` event_id 不得相交；两组所有可比较 segment 共享 factor-specific landmark offset 和 20 日 horizon。holding 两臂 event_id 集合逐项相同。F4/F2 diagnostic denominator 不得改变这些计数。

顶层 unavailable 时四因子结果、events、segments、统计均为空；provenance 可为空或只包含已验证 identity，不得返回部分可信结果。

## 7. 实施顺序

- 主会话先冻结 `models.py` 中 landmark、event group、共同现金路径和响应不变式。
- detector 只负责 parent/qualification/landmark evidence；不计算收益。
- evaluator 独占可达性派生、PIT universe 三态、共同时钟、统计与 verdict，避免四检测器各自实现执行口径。
- API adapter 只拥有 reader 生命周期和 Pydantic 映射。
