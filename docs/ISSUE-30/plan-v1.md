# ISSUE-30 v1 实施方案：日线开盘价锚定过滤器

关联：[Issue #30](https://github.com/wf2311/fm-workbench/issues/30) · [README](README.md) · [可行性评估](feasibility.md)  
状态：**契约冻结草案，待 review-v1**。本文件冻结“实现什么、如何统计、何时拒绝”，不填入任何回测结果。

## 0. 研究问题与 verdict

- H1：原稿过滤臂在 OOS 的 `stop_hit_rate` 低于无过滤臂。
- H2：原稿过滤臂在 OOS 的 `expectancy` 不低于无过滤臂。
- 只有 OOS 可产生 verdict；IS 仅披露。`n_trades` 的 OOS 最小门槛为 30（none 与 original 均须达到）。
- `validated` = H1、H2 同时成立且达到样本门槛；`rejected` = OOS 任一核心条件失败；`inconclusive` = 样本不足或删失使比较不可判定；`pending` 仅表示尚未运行，不是研究结论。
- 不输出“收益保证”或“降低止损频率”先验主张。

## 1. 冻结常量

| 项目 | v1 值 | 性质 |
|---|---|---|
| definition/factor id | `daily_open_anchor_filter_v1` | 本研究冻结 |
| schema version | `1` | 本研究冻结 |
| 市场/方向 | A 股现货，多头 | Issue 范围 |
| 信号 | MA5 上穿 MA20，T 收盘确认 | 日频代理，模块内独立实现 |
| 退出 | MA5 下穿 MA20；止损 `-0.06`；最长持仓 `15` 日 | 与 `ma_golden_cross.py:20-22` 语义一致 |
| 建仓/清仓 | `entry_fill=open_t+1`；`exit_fill=close_t` | T+1/PIT |
| 费用 | `fees_pct=0.0002`、`slippage_bps=5.0`、`stamp_tax_pct=0.0005`（卖出单边） | `MatcherConfig` 默认值，`engine.py:41-51` |
| 锚年龄上限 | 20 个交易日 | 本研究冻结选择 |
| 最大标的数 | 200 | Issue #30 |
| 最大窗口 | 370 个交易日 | Issue #30 |
| 趋势桶 | 近 5 日累计变化 ≥+3% 单边上涨；≤−3% 单边下跌；其余震荡 | 本研究冻结选择 |
| gap 桶 | T+1 open 相对 T close：>0 高开、<0 低开、=0 平开 | 诊断分层 |
| 距锚桶 | `abs(price/anchor-1)`：<2% 近锚、2–5% 中距、≥5% 远锚 | 诊断分层 |
| 随机种子 | `sha256(symbol + "|" + signal_date.isoformat())` 前 8 hex 转整数 | 禁用 Python 内建 hash |
| OOS 最小交易数 | 30（none/original 各自） | verdict 门槛 |

## 2. 数据与 reader 契约

### 2.1 来源与所有权

请求必须装配两个 published reader：

1. `PublishedCanonicalDailyReader`（`backend/app/services/research_sealed_data.py:14`）：构造时 pin 一个 canonical generation，取前复权 `open/high/low/close`、raw 证据列和交易日历。
2. `PublishedDailyMarketFactsReader`（`backend/app/data_providers/fquant/daily_market_research.py:17`）：读取同请求固定的 markets generation，提供 PIT 涨跌停/停牌制度事实；`limit_regime_facts` 的协议形态见 `backend/app/services/n_shape_golden_phoenix.py:89-96`。

生产侧仿照 `weak_to_strong_research_data.py:206` 的 `production_reader_scope`：reader 为请求所有，`finally` 关闭；不得让下一请求隐式复用上一请求 reader 或跟随 `current.json`。

### 2.2 canonical panel

服务将日线与 PIT facts 合并为按 `symbol,date` 排序的 panel，至少包含：

`symbol`, `date`, `open`, `high`, `low`, `close`, `signal_limit_up`, `signal_limit_down`；`volume` 可选但不用于放宽数据门。OHLC 使用同一 pinned generation 的前复权列，raw 值仅作 provenance/evidence。

`signal_limit_up/down` 必须来自精确 PIT 涨跌停事实；事实缺一不可。不能因缺列而使用 engine 的无约束默认分支。合并后 panel schema 必须在服务入口校验。

### 2.3 fail-closed 与删失

- canonical reader 缺失、manifest 不是合法 64 位身份、generation mismatch：整单 `status=unavailable`。
- 请求范围任一 `symbol × date` 缺 PIT limit facts：整单 `unavailable`，不得退化为“无涨跌停限制”。
- 个股无日线、必需字段缺失/非法、个股 generation mismatch：该标的加入 `censored`，其余标的可继续。
- 无近 20 个交易日阳线：事件记 `anchor_unavailable`；保留在信号全集计数，但不为原稿/反向/随机臂伪造锚。
- 下一交易日/执行 bar 缺失：事件级 `censored`。
- 停牌、涨跌停阻塞是撮合结果，不被误记为过滤臂主动过滤。

## 3. 信号契约

- 只用已收盘日线计算 MA5、MA20；`MA5[t-1] <= MA20[t-1]` 且 `MA5[t] > MA20[t]` 才生成 signal date T。
- T 收盘后信号冻结，任何 T+1 数据不得参与信号或过滤决定。
- T+1 下一可交易日开盘执行。模块内独立实现 MA5/20，不 import 依赖 enriched 的策略引擎；`ma_golden_cross.py:20-22` 仅作为 STOP_LOSS/MAX_HOLD/死叉语义的参考冻结源。
- 退出信号为 `MA5[t-1] >= MA20[t-1]` 且 `MA5[t] < MA20[t]`，按 `exit_fill=close_t`；止损与最长持仓由既有撮合器执行。
- 所有四臂先从同一个 signal set 生成 mask；不得让各臂分别生成信号或使用不同数据覆盖。

## 4. 锚契约

1. 对 signal date T，候选锚只能来自 `date < T` 的已收盘 bar；阳线定义 `close > open`。取最近一根阳线的 open。
2. 比较值和锚值同用 pinned generation 的前复权口径。raw 值可以随事件披露，但不得参与比较。因为同日 OHLC 的调整是同一正比例缩放，`close > open` 的阳线判定不因复权改变。
3. 锚的 `age_trading_days` 按 pinned market calendar 计，不按自然日。超过 20 个交易日或找不到阳线 → `anchor_unavailable`。
4. 对固定 `(symbol, signal_date, generation)`，锚日期必须唯一；新增一根更近阳线后只在该信号日重算时切换，不能在同一事件中抖动。
5. 随机臂从 signal date 之前、可用候选窗内的已收盘 bars 用 sha256 种子均匀选一根 open 作为 `random_anchor`；同输入必须选同一根，禁止内建 `hash()`。

## 5. 四臂与决策时间

| arm | 锚/掩码定义 |
|---|---|
| `none` | 所有有效 signal 保留，作为无过滤基线 |
| `original` | `price_close_t < anchor_value` 时保留；原稿“现价低于锚”的日频可实现代理 |
| `inverted` | `price_close_t >= anchor_value` 时保留；与 original 对有效有锚事件互补 |
| `random` | 使用 deterministic random anchor，再套用同一 `<` 规则 |

**主决策价必须是 T close**：这是信号确认时已知的价格，可在无未来函数条件下实现。T+1 open 与锚的比较作为执行时点描述性口径并列披露，不能反过来生成 T 的入场 mask；这也解释了与原稿盘中口径不同的降档。高开/低开层使用 `open_t+1 - close_t`，距锚层分别使用决策价和执行价并标明 basis。

每一事件必须同时记录四臂 retained/filter decision；被过滤事件仍属于 signal universe，不能从分母删除。

## 6. 撮合与统计契约

### 6.1 撮合路径

构造 panel 与 entries/exits 后，四臂均调用 `BacktestEngine.simulate_independent_candidates`（`backend/app/backtest/engine.py:782-791`），不自写第二套撮合。该路径是每个候选独立样本，适合比较过滤器而非模拟组合容量。

- `MatcherConfig(entry_fill="open_t+1", exit_fill="close_t", stop_loss_pct=-0.06, max_hold_days=15, fees_pct=0.0002, slippage_bps=5.0, stamp_tax_pct=0.0005)`。
- T+1 当日不触发风控：`engine.py:963-965` 的 `entry_idx == idx` 分支。
- 开盘已跳空穿越止损线按开盘价成交，否则按止损线价：`engine.py:987-994`。
- 涨停禁买、跌停禁卖/停牌阻塞：`engine.py:939-960`；pending exit 与 `blocked_exit_days`：`engine.py:1010-1017`。
- 成本、退出原因、MAE/MFE、胜率/盈亏比/交易数/换手等既有统计继续复用（`engine.py:1883-1919,2143-2146`；expectancy/payoff 定义见 `metrics.py:504,532-533`）。
- `simulate_independent_candidates` 接收已构造 panel；测试必须证明使用 `BacktestEngine(repo=None)` 仍可运行，不能隐式读取 repo 或 enriched。

### 6.2 过滤样本虚拟结局

无过滤臂是唯一虚拟结局来源。以 `(symbol, entry_signal_date)` 将 none 臂交易与每个 arm 被过滤事件连接：

- none 臂确实成交：复制其成本后 pnl、exit reason、MAE/MFE 作为被过滤事件的 `virtual_outcome`，并标注 `source=none_arm`。
- none 臂因涨停/停牌/缺未来 bar 未成交：虚拟结局为 `censored`，保留阻塞原因；不能伪造收益。
- arm 主动过滤与 engine 执行阻塞必须分列统计。

### 6.3 stats 白名单与研究红线

每臂只允许以下 stats 键：

`n_signals`, `n_retained`, `n_filtered`, `n_trades`, `stop_hit_count`, `stop_hit_rate`, `win_rate`, `avg_win`, `avg_loss`, `payoff_ratio`, `expectancy`, `avg_mae`, `avg_mfe`, `max_drawdown`, `turnover`, `cost_total`, `net_pnl_pct_mean`, `exit_reason_counts`, `block_counts`。

`stop_hit_rate = exit_reason_counts["stop_loss"] / n_trades`，无交易时为 null，不伪造 0。此 stats 白名单是新“入场过滤器对照研究”子家族，必须在 review-v1 明确签字；既有 evidence 层的 `BANNED_EVIDENCE_KEY_TERMS`（`weak_to_strong.py:23`）与 `_BANNED_TRADING_TOKENS`（`volume_breakout.py:33-36`）继续生效。事件 evidence/censoring 和顶层不得出现订单、买卖方向、仓位或动作建议。payload 固定免责声明：仅统计性研究输出，非投资建议。

## 7. 分层契约

每个 arm 的 stats 同时按以下层输出，禁止只给全样本均值：

1. `trend_bucket`：每标的信号日前 5 交易日累计涨跌幅三桶（≥+3%、≤−3%、其余）。单边下跌桶必须单列。
2. `gap_bucket`：T+1 open 相对 T close 的高开/低开/平开。
3. `anchor_distance_bucket`：决策价相对锚的绝对距离（近/中/远）。

这些分层只用于诊断，不回灌 mask。既有 `regime_breakdown.py:1-19,164-165` 已明确事后分层的前视披露范式；v1 仍须披露分层生成时点和任何删失，不能将其包装成可交易 regime signal。

## 8. API 契约

### 8.1 能力 GET

`GET /api/research/daily-open-anchor`

返回固定定义、`available`、不可用原因、`schema_version`、限制 `{max_symbols:200,max_window_trading_days:370,anchor_max_age_trading_days:20}`、sealed 数据门（包括盘中 `unavailable`）。不运行回测。

### 8.2 求值 POST

`POST /api/research/factors/daily-open-anchor/evaluate`，沿用 `api/research.py:387-398,415-423` 的 capability + factor evaluate 约定。

请求严格为：

```json
{"start":"YYYY-MM-DD","end":"YYYY-MM-DD","oos_start":"YYYY-MM-DD","symbols":["..."]}
```

约束：`start <= oos_start <= end`；symbols 非空且不超过 200；按 pinned market days 计算窗口且不超过 370 个交易日。违反约束返回 400。reader/能力缺失返回 200 的 `status="unavailable"` 结构并给 reasons；生产 reader 非预期错误映射 503，禁止返回半可信结果。

### 8.3 响应结构

固定顶层：

```json
{
  "factor_id":"daily_open_anchor_filter_v1",
  "schema_version":1,
  "status":"ok|unavailable",
  "reasons":[],
  "definition":{},
  "provenance":{"generation":"...","manifest_sha256":"...","oos_start":"...","market_days_count":0,"limits":{}},
  "events":[],
  "arms":{"none":{"stats":{},"layers":{}},"original":{"stats":{},"layers":{}},"inverted":{"stats":{},"layers":{}},"random":{"stats":{},"layers":{}}},
  "verdict":{"label":"validated|rejected|inconclusive|pending","basis":"oos","rules":[]},
  "censored":[],
  "disclaimer":"研究对照输出：仅统计性执行结果，不含交易指令、买卖方向或投资建议"
}
```

事件至少含 `symbol`, `signal_date`, `anchor{date,value,age_trading_days,basis}`, `decision{price_close_t,basis,retained_by}`, `random_anchor`（适用时）、`execution`、none 臂结果/虚拟结局、`segment`, `layers`, `execution_block`, `censoring`。不返回订单、方向、仓位或价格目标字段。

响应不得含墙钟 `observed_at` 等不稳定字段；相同输入、相同 pinned generation 的事件/arms/verdict/provenance 必须逐字节一致。manifest、generation、OOS 起点和删失来源必须可追溯。

## 9. 测试矩阵

| 夹具 | 必须断言 |
|---|---|
| 阳线锚 | 只取信号日前收盘阳线，锚日期/年龄正确 |
| 阴线锚 | 最近阴线跳过，取更早阳线 |
| 20 日无阳线 | `anchor_unavailable`，不伪造锚，计入全集 |
| 高开/低开/平开 | gap 三桶边界正确 |
| 单边下跌 | 5 日累计 ≤−3% 进入单边下跌层且该层非空 |
| 跳空穿越锚/止损 | T+1 open 口径并列披露；穿止损时按 open 成交 |
| 新锚切换 | 新阳线出现后锚唯一前移，固定事件无抖动 |
| 无信号日 | 零事件、四臂零信号 |
| T+1 当日止损 | 入场日跌破线不触发当日风控 |
| 涨停禁买 | `buy_limit_up` 阻塞，不伪造成交 |
| 跌停/停牌禁卖 | pending exit 与 blocked days 正确累计 |
| canonical/raw/generation 缺失 | fail-closed 或对应 censored 原因明确 |
| PIT facts 缺失 | 整单 unavailable，不走无约束默认 |
| panel schema | 缺 limit flags 时拒绝，不静默继续 |
| 随机确定性 | 同输入两次 payload 逐字节一致，种子不依赖内建 hash |
| original/inverted 互补 | 对有效有锚事件互斥且并集为全集 |
| 虚拟结局 | 被过滤样本由 none 臂 join 得到非空结果；none 未成交则 censored |
| repo-free | `BacktestEngine(repo=None)` 的独立候选模拟可运行 |
| OOS verdict | 达标 validated、恶化 rejected、少于 30 inconclusive；IS 达标不能单独 validated |
| 参数边界/API | symbols>200、窗口>370、日期非法返回 400；能力缺失返回 unavailable |
| reader 生命周期 | 请求结束关闭两个 reader |

## 10. 验证与审阅门

实现阶段按 Issue #30 顺序执行：确定性夹具 → production reader 小样本冒烟（不超过 5 标的、60 交易日）→ focused tests → backend 全量回归与 Ruff F/E9 → 独立 coding review。review-v1 必须特别审查 stats 白名单例外、T close 决策无未来函数、PIT 缺失 fail-closed、虚拟结局 join 和 OOS verdict；review-v2 后才能形成 final-design/verification。

本波仅建立 [README](README.md)、[feasibility](feasibility.md) 与本方案，未改代码、未写 `data/`、未运行测试或构建。

## 11. 明确非目标

盘中分钟、空头/期货/期权/融券、FVG/OB、前端、策略池/optimizer/Agent、真实交易、全市场生产回测、任何订单或买卖建议输出，均不在 v1。
