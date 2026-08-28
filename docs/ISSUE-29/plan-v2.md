# ISSUE-29 v2 实施方案：左一K线防守位

> 状态：`plan-v1` 经独立审查拒绝；本稿逐条修复 R1–R9，待二次 review。
> 基线：`workbench/feature/fstore-engine-duckdb-source` @ `7bf2982`。
> 上位议题：[GitHub Issue #29](https://github.com/wf2311/fm-workbench/issues/29)；文档导航：[README.md](README.md) · [feasibility.md](feasibility.md) · [review-v1.md](review-v1.md) · [plan-v1.md](plan-v1.md)。

## 1. v2 的收缩原则

v1 的参数变体全部从请求模型删除。v2 只实现一套不可变配置：中位线 `window=3`；同价高点取最新；strict 完全包含；破位后次日收回不撤销；ATR 吊灯 `k=3`。任何变体必须另建带独立 RunCard 的研究 run，不能在同一 OOS 请求中试选。

这是 adjusted-price hypothetical research：复权价格用于形态、防守线和经济收益归一化；`raw_*` 只用于可成交报价、限跌停证据和 execution evidence。复权 `open` 绝不称为“实际报价”或“实际成交价”。

## 2. 唯一 common entry predicate（修 R2、R6）

对每个 symbol 按已完成市场日计算：`uptrend(T) = close_adj(T) > MA60_adj(T) AND MA20_adj(T) >= MA60_adj(T)`。唯一入场事件是 `uptrend(T-1)=false` 且 `uptrend(T)=true` 的 T 收盘 transition；首个可计算状态因 MA60 暖机不足而不可形成时不造 entry。

- `entry_id` 固定为规范化 `symbol + signal_date(T)`，全六臂共用并在每臂结果回显。
- T+1 必须是 pinned market calendar 的精确下一个交易日；按该日 open 建立入场。缺 T+1 bar、不可买或缺少必要报价时，该 entry 返回明确 censor code，不向后寻找替代入场日。
- 同一 symbol 已持仓或处于 `pending_exit` 时，后续 transition 不产生新 entry；退出或 horizon 结束后才允许下一次 transition entry。
- 入场 predicate 只由上涨状态 transition 决定，任何离场臂不得反向筛选 entry。六臂因此逐 `entry_id` 对齐。

## 3. 数据、PIT 与可卖性（修 R1、R5）

### 3.1 两套价格账本

1. **研究账本**：同一 generation 的前复权 `open/high/low/close`，用于上涨状态、MA/ATR、左一形态、防守线、各段归一化收益、MAE/MFE。
2. **可成交证据账本**：同一 generation 的 `raw_open/raw_high/raw_low/raw_close`，仅记录下一可卖日的报价证据、跳空关系和 execution evidence；不把 raw 值混入复权线或形态计算。payload 字段命名必须区分 `research_*_adj` 与 `quote_*_raw`，不得输出含糊的“actual adjusted price”。企业行动下采用定义固定的复权经济收益，不声称 raw 报价与 adjusted 数值相同。

### 3.2 generation 与 PIT 限跌停

只读 `PublishedCanonicalDailyReader` 的单一 generation，并在 provenance 固化 generation、manifest_sha256、columns、market_days。`turnover_rate` 禁用；不读写 `data/`，不跨 generation 合并。

一字跌停可卖性不是仅由 OHLC 猜测：必须由同 generation、PIT 的 markets reader 提供限跌停 signal，并与当日 raw OHLC 同价条件精确构造 `sellable=false`。该 markets reader 输入、字段和构造结果列入 required columns 与 provenance。markets reader、限跌停 signal 或必要 OHLC 任一缺失时，**整个 evaluate 返回 `unavailable`**（不只是跳过某 entry），不得把不可卖日当成可成交 open。

## 4. 状态机与防守线（修 R9）

### 4.1 左一规则

截至 T 的完成 bars 内，在最近 3 根窗口取 `high_adj` 最大值；同价取最新。由中位线向左最多搜索 10 根，第一根不被完全包含者为左一：A 被右侧 B 包含当且仅当 `A.high_adj <= B.high_adj AND A.low_adj >= B.low_adj`，等点也算包含。找不到左一即无防守位、无该臂离场信号，不报错。

防守位 = 左一 `low_adj`。入场日不离场（T+1）；持仓中 `close_adj(T) < defense_line_adj` 才确认破位，等于线是触碰。确认信号后只在下一可交易市场日寻找可卖 open：跳空关系记在 raw quote evidence，收益仍以研究账本规则归一化。

### 4.2 uptrend_lost 转移

持仓中若 `uptrend` 从 true 变为 false（`close_adj <= MA60_adj` 或 `MA20_adj < MA60_adj`），转移到 `uptrend_lost`：保留最后有效防守线，停止其上移/重算，直到出现 `close_adj` 创入场后新高且上涨状态恢复；恢复后才重新计算并允许防守线抬高。`uptrend_lost` 不自动退出，保留的线仍照常检查收盘破位；证据记录状态转移、保留线和恢复日。已确认破位不因次日收回撤销（v2 唯一配置）。

停牌或由 markets reader 精确判定的一字跌停时进入 `pending_exit`，延后到首个可卖市场日；数据到 horizon/样本末端仍不可卖则按相应右删失规则处理。未完成 bar 永不参与任何状态或重算。

## 5. 六臂与共同观察 horizon（修 R3、R6）

每个 `entry_id` 对六臂使用同一个 T+1 入场和**共同 60 个交易日 observation horizon**；所有结果按 entry_id 对齐，不以某一臂的离场日作为另一臂终点：

1. buy-and-hold；
2. ATR 吊灯：`max_high_adj - 3 * ATR14_adj`；
3. MA20 持有；
4. MA60 持有；
5. 左一防守位；
6. 左一 + ATR 复合，取两条防守线较高者。

完整 horizon 之前触发离场的臂在其离场后以研究规则记录结果，并在共同 horizon 结束时记录该段后续状态；未触发离场者在 horizon close 结算。若 horizon 不足（数据截断、末端或缺 bar），该 `entry_id`/臂标记 `horizon_incomplete`，不得静默删除；其是否进入某项统计的分母在 payload 中单列。pending_exit 未完成时不伪造成交价，标记 `pending_exit_censored`。

## 6. 逐段统计（修 R3）

删除年化收益、Sharpe、组合 MaxDD 和等权组合 NAV：不同进出场日的段不能伪装为等频组合收益序列。v2 只报告逐 `entry_id`、逐臂的预先定义样本统计：

- horizon/离场结算的持有期净收益（成本后；研究账本归一化）；
- MAE（持有段内相对入场研究价的最差回撤）；
- MFE（持有段内相对入场研究价的最大有利变动）；
- 持有交易日数；
- 卖飞率：离场后固定 `N ∈ {5,10,20}` 三个 horizon，逐项输出；
- 破位后下跌深度：离场/破位后固定 `M=5` 个交易日的 `min(close_adj)/exit_research_value - 1`；
- 防守位距离的 ATR 分位与换手诊断（不用 `turnover_rate`）。

卖飞率的 rolling-max 固定为离场前持仓段截至离场日的累计 `max(close_adj)`，端点包含离场日；未来 N 日窗口端点包含第 N 个交易日。破位深度窗口端点包含第 M 个交易日。任一诊断 horizon 不完整时单独标记 `diagnostic_horizon_incomplete`，不进入该指标分母；payload 同时给出 eligible/censored 分母。bootstrap/Wilson 只能应用于这些逐段比例或分布的预先定义摘要，不能重新构造组合路径指标。

每边成本 `cost_bps=10` 固定默认且请求可显式传入合法值；净收益扣除入场和离场各一边，无法成交的 pending 段不凭空扣离场成本。

## 7. 完整请求与响应契约（修 R8）

请求模型 `extra="forbid"`，字段冻结如下：

| 字段 | 类型/约束 |
|---|---|
| `symbols` | 非空 symbol 数组；规范化交易所代码、去重，最多 500 个；超限/非法格式 → HTTP 400 |
| `start` | ISO 日期，包含；必须早于 `oos_start` |
| `end` | ISO 日期，包含；必须满足 `oos_start <= end` |
| `oos_start` | 必填 ISO 日期；IS 为 `[start, oos_start)`，OOS 为 `[oos_start, end]` |
| `cost_bps` | 默认 10；非负且不超过 1000；非法值 → HTTP 400 |

不接受任何 window/tie/include/recovery/ATR-k 变体字段。请求校验失败（日期、symbols、未知字段、成本）→ HTTP 400；请求合法但 reader/required columns/markets PIT 输入缺失 → HTTP 200 业务 `unavailable` + reasons。响应固定含 `definition_version=v2`、请求规范化结果、参数快照、entry_ids、六臂逐段结果、censored、IS/OOS 样本分母、统计摘要、diagnostics、verdict 和 provenance。

`GET /api/research/zuoyi-defense` 返回同一 capability/definition/required columns；`POST /api/research/factors/zuoyi-defense/evaluate` 执行评估。两端都只能从 repository 的 generation-pinned reader 取数据。

## 8. Verdict 与治理

verdict 只看 OOS 的 entry_id 对齐逐段统计；无真实结果不预填 accepted。样本不足、horizon/markets 数据不可用或 required provenance 缺失 → `unavailable`；达到预设最低样本量但相对最佳基准无稳定增量 → `rejected`；只有预先登记、可复核且 OOS 稳定增量才可 `accepted`。所有参数、样本分母、删失和统计版本登记到 `research_registry.py` 的 Hypothesis/RunCard；禁止以原稿成功案例替代 OOS 证据。

## 9. 测试与验证契约

确定性测试必须覆盖：同价最新 tie-break；完全/等点包含；下影触碰收回；收盘破位次日收回不撤销；跳空 raw quote evidence 与 adjusted research value 分离；震荡多段但 transition/持仓去重；停复牌 pending/censor；markets limit-down 缺失导致整单 unavailable；除权前后复权连续性；缺列/暖机不足；uptrend_lost 保线停移及恢复；共同 60 日 horizon、entry_id 对齐、horizon/diagnostic censor；截断不变性（截去 T 之后，T 时点防守位和 entry predicate 不变）。实现波须跑完整测试文件、真实 reader 小样本冒烟、后端回归与 Ruff F/E9，并经独立 review；本方案波不执行这些命令。

## 10. R1–R9 修复映射

| Finding | v2 修复 |
|---|---|
| R1 限跌停输入 | §3.2：markets reader 同 generation/PIT 精确构造；缺失整单 unavailable |
| R2 common entry 未定义 | §2：false→true T 收盘、精确 T+1、entry_id、不可买 censor、持仓/pending 去重 |
| R3 组合指标无时间轴 | §6：删除年化/Sharpe/组合 MaxDD/NAV，逐段统计并给删失分母 |
| R4 诊断窗口不完整 | §6：N={5,10,20}、M=5、端点规则、horizon censor/provenance |
| R5 复权与成交混淆 | §1/§3.1：adjusted 研究账本、raw 报价证据，禁称 adjusted open 实际报价 |
| R6 共同终点缺失 | §5：共同 60 交易日、entry_id 对齐、horizon 不足单独删失 |
| R7 参数可变导致 OOS 泄漏 | §1：window=3/latest/strict/no-cancel/k=3 单一不可变配置 |
| R8 请求模型不完整 | §7：完整字段、默认/允许值、日期与 symbol 校验、400/unavailable 边界 |
| R9 uptrend_lost 未定义 | §4.2：保留最后线、停止上移，创新高且状态恢复后才恢复 |

## 11. 非目标与红线

15m/1h、生产回测引擎改造、前端 UI、真实交易/下单、策略池、监控、生产调度和任何 `data/` 写入仍非目标。必须遵守 sealed canonical、单 generation、严格 PIT、fail-closed、A 股 T+1；研究服务是可审计的 adjusted-price hypothetical 实验，不是成交系统。
