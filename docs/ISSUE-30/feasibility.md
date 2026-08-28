# ISSUE-30 可行性评估

关联：[Issue #30](https://github.com/wf2311/fm-workbench/issues/30) · [README](README.md) · [v1 方案](plan-v1.md)  
基线：`7bf2982`。本文件只压缩评估事实，不包含回测结果。

## 结论

**可行（有条件）**。在「日频收盘确认信号 + 下一可交易日开盘执行」契约下，可以使用 sealed canonical 日线、published markets PIT 事实和现有独立候选撮合路径完成可审计对照。原稿字面上的“盘中小周期信号出现时，现价与锚比较”不可作为 v1 事实：AGENTS.md:54 记录的首个 ordered-trans generation 仅覆盖 `600519.SH`、`000001.SZ`、`300750.SZ` 各 30 个完整日。因此盘中能力必须显式 `unavailable`，不能用未 sealed 的 `provider.get_minute` 替代。

## 一、数据事实

1. `PublishedCanonicalDailyReader`（`backend/app/services/research_sealed_data.py:14`）在构造时固定一个 published canonical generation，不跟随 `current.json`；应提供 `generation`、`manifest_sha256`、`market_days`、`daily_bars`、列能力等身份信息。
2. canonical/enriched 的存储列约定（`backend/app/indicators/pipeline.py:67-76`）同时存在前复权 OHLC 与 `raw_open/raw_close/raw_high/raw_low`。跨除权日的锚比较必须统一使用同一 pinned generation 的前复权值；raw 值只能作证据，不能与前复权值混比。
3. `PublishedDailyMarketFactsReader` 位于 `backend/app/data_providers/fquant/daily_market_research.py:17`，读取一个 published markets generation；`limit_regime_facts` 的 reader 协议/转发形态见 `backend/app/services/n_shape_golden_phoenix.py:89-96` 与 `n_shape_research_data.py:70-72`。PIT 精确涨跌停事实是必需输入，缺失时整单 fail-closed。
4. `AGENTS.md:54` 明示 ordered-trans 试点覆盖只有 3 个标的 × 30 个完整日，故不能支撑全量盘中回测。
5. `canonical_history.py` 的 `get_daily(["000001.INDEX"], ...)` 调用（约 553-560 行）只用于日历校验，不能据此声称具备 sealed 市场级指数状态历史。v1 的趋势/震荡改用逐标的、信号日前可计算的 5 日分层。

## 二、可复用 seam

| 事实/能力 | 现有落点 | 接入约束 |
|---|---|---|
| generation-pinned canonical | `services/research_sealed_data.py:14` | 请求内固定 generation 与 manifest，禁止跟随 current |
| reader 生产装配 | `services/weak_to_strong_research_data.py:206` | 仿照 `production_reader_scope`，请求级所有权并在 finally 关闭 |
| PIT 市场制度事实 | `data_providers/fquant/daily_market_research.py:17`；`services/n_shape_golden_phoenix.py:89-96` | 精确涨跌停价/停牌事实缺失整单 unavailable |
| 独立候选撮合 | `backtest/engine.py:782-791` | `simulate_independent_candidates(panel, entries, exits, config)` 每个候选独立执行，不受组合资金限制 |
| T+1 与成交列 | `backtest/engine.py:816-820`、`engine.py:841-843` | `entry_fill=open_t+1` 将 T 信号移到 T+1 开盘；退出单独使用 close_t |
| T+1 风控禁用 | `backtest/engine.py:963-965` | `_risk_exit` 对 `entry_idx == idx` 跳过当日风控 |
| 涨跌停/停牌阻塞 | `backtest/engine.py:939-960`、`1010-1017` | 买入涨停、卖出跌停/停牌进入阻塞与 pending_exit 统计 |
| 跳空止损 | `backtest/engine.py:987-994` | 开盘已穿线按开盘成交，否则按止损线成交 |
| 成本与统计 | `backtest/engine.py:41-51`、`1883-1919`、`2143-2146`；`backtest/metrics.py:504,532-533` | 费用、滑点、卖出印花；win rate/payoff/expectancy/turnover 等可复用 |
| 研究能力/删失/provenance | `services/single_yang_no_break.py:13-18,75-83,124-127,216,243-249` | 仿照 capability、censored、IS/OOS、manifest 结构 |
| 研究免责声明/字段红线 | `services/weak_to_strong.py:15,22-23`、`volume_breakout.py:33-36` | stats 层设显式白名单，evidence 层仍禁交易语义键 |
| 分层前视披露 | `backtest/regime_breakdown.py:1-19,164-165` | 事后诊断只能披露，不能回灌交易信号 |
| API 形态 | `api/research.py:387-398,415-423` | 沿用 capability GET + `/factors/<id>/evaluate` POST |

## 三、缺口与降档决策

1. **盘中缺口**：分钟覆盖不足且非 sealed。降档为 MA5/20 日频收盘代理；盘中口径返回 `unavailable`。
2. **市场级趋势缺口**：没有可用于此研究的 sealed 指数日 K。降档为每标的信号日前 5 交易日净变化三桶，并单列单边下跌失败层。
3. **复权陷阱**：跨除权日 raw 锚会产生伪穿越。锚值与比较价均取同 generation 的前复权列；同日 `close > open` 的阳线判断在同一正比例调整下不变。raw 仅披露。
4. **锚点陈旧**：最近阳线可能过旧。冻结 20 个交易日年龄上限；无近 20 日阳线记 `anchor_unavailable`，进入信号全集计数，不静默补锚。
5. **交易统计红线**：既有 evidence payload 禁止 `buy/sell/entry/exit/stop_loss` 等键（`weak_to_strong.py:23`、`volume_breakout.py:33-36`）。本研究新增“入场过滤器对照研究”子家族：仅 stats 层允许经 review-v1 签字的统计白名单；evidence/censoring 层和顶层仍不得输出订单、方向、仓位或动作建议。
6. **撮合组合风险**：`simulate_independent_candidates` 与 sealed panel 是新组合，必须用 panel schema、T+1、跳空、PIT 阻塞夹具锁定，不自行实现第二套撮合。

## 四、明确非目标

- 空头、期货、期权、融券：A 股现货 sealed 输入和该引擎路径只有多头候选语义。
- FVG/OB：Issue #30 未要求本轮同时冻结，基线亦无可复用实现。
- 盘中分钟信号：数据门不满足，不能以 tdx-minutes 或 provider fallback 补齐。
- 前端 UI、策略池、optimizer、Agent、真实交易：本轮只产出可审计研究服务契约。
- 写 `data/`、生成全市场生产回测或宣称收益：均不属于 feasibility 可行性证明。

## 五、数据门与 fail-closed

| 条件 | 结果 |
|---|---|
| canonical reader 缺失、manifest 非法、generation 身份不一致 | 整单 `unavailable` |
| 任一请求范围的 PIT 涨跌停事实缺失 | 整单 `unavailable`，不得退化为无涨跌停约束 |
| 个股日线为空、字段缺失或数值非法 | 该标的 censored，其余标的可继续 |
| 20 交易日无阳线锚 | 事件 `anchor_unavailable`，不伪造锚 |
| 下一可交易日或执行 bar 缺失 | 事件 censored |
| 盘中能力请求 | `unavailable`，附 sealed 覆盖原因 |

## 六、风险与结论纪律

- 研究只能回答冻结口径下的样本事实；不把单案例或原稿“降低止损频率”主张当作先验结果。
- verdict 必须只使用 OOS；IS 只作诊断。趋势/震荡、高开/低开、距锚距离必须分层展示，尤其不能用全样本均值掩盖单边下跌的接飞刀风险。
- 任何无法追溯到 generation、manifest、PIT facts 来源或输入删失原因的结果均不具备发布资格。
