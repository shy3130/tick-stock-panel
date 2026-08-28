# ISSUE-30 plan-v2：评审修订后的实施契约

关联：[Issue #30](https://github.com/wf2311/fm-workbench/issues/30) · [README](README.md) · [feasibility.md](feasibility.md) · [plan-v1.md](plan-v1.md) · [review-v1.md](review-v1.md)  
状态：**逐条回应 review-v1；仍待 review-v2**。除本文明确修订外，plan-v1 条款继续有效。本波仍只改文档，不改公共代码、不运行测试。

## 0. R1–R5 修订总览

| finding | v2 修订位置 | 结果 |
|---|---|---|
| R1 PIT 下限事实缺失 | §1 | 扩展私有 research reader；pre_close/制度事实/舍入重建上下限；缺一整单 unavailable |
| R2 无逐候选执行结局 | §2 | 服务侧 execution ledger；engine 只撮合可执行候选；ledger 记录每个未成交原因；不改公共 BacktestEngine API |
| R3 相邻行 shift 猜 T+1 | §3 | pinned calendar 先算精确 T+1；缺 bar/停牌事件 censor/blocked；不把后续可用 bar 当 T+1 |
| R4 IS/OOS 未进入 arms 结构 | §4 | 每臂 `segments.is/oos`，按 signal_date 归段；verdict 只读 OOS |
| R5 独立候选伪组合统计 | §5 | 移除组合 turnover/cost_total/MaxDD 承诺；冻结 candidate-sample 净收益、MAE/MFE、stop-hit、blocked/censored 定义 |

## 1. R1：PIT 涨跌停上下限重建

### 1.1 现状与不可降级原则

基线 `PublishedDailyMarketFactsReader.limit_regime_facts`（`backend/app/data_providers/fquant/daily_market_research.py:160-185`）当前只暴露 `limit_up_price`、`name`、`is_st`、`regime`；它没有 `pre_close`、跌停价或停牌字段。不得填 `signal_limit_down=False`，也不得退化为无约束卖出。

v2 不修改公共 BacktestEngine API。实现前先扩展**私有 research reader 契约**（可在 reader 内部增加私有方法或由 research wrapper 提供）：

```python
limit_band_facts(symbol, start, end) -> dict[date, {
    "pre_close": float,
    "published_limit_up_price": float,
    "regime": str,
    "is_st": bool,
    "name": str,
    "board": str,
    "suspended": bool | None,
}]
```

若 markets generation 无法提供 PIT `pre_close`，或无法对每个日期给出制度/板块/ST 事实，则能力为整单 `unavailable`；不得用相邻收盘、当前 ST 状态或当前板块信息代替。若 `suspended` 不在 markets generation，日历有交易日但 canonical 无 bar 只按 §3 记 `blocked/censored`，不声称已取得停牌事实。

### 1.2 重建公式与一致性校验

制度比例从 pinned PIT `regime`/板块规则冻结为：`main_10=10%`、`st_5=5%`、`chinext_20=20%`、`star_20=20%`、`beijing_30=30%`。历史生效日沿用 reader `_regime`（`daily_market_research.py:148-158`），并以测试固定边界日期。

使用 Decimal `ROUND_HALF_UP`，以元为单位保留两位小数：

```text
upper = round_half_up(pre_close * (1 + ratio), 0.01)
lower = round_half_up(pre_close * (1 - ratio), 0.01)
```

`published_limit_up_price` 必须与 `upper` 一致；不一致即 markets facts 内部冲突，整单 `unavailable`。`signal_limit_up = high >= upper`、`signal_limit_down = low <= lower`；engine 的 one-price/same-price 检查继续负责一字板阻塞。所有 flag 在调用 engine 前构造，缺事实不得走默认全 False 分支。

测试必须覆盖主板、ST、创业板生效日、科创板生效日、北交所生效日、两位小数边界、上限交叉校验、缺 pre_close、缺 regime/board/ST、上下限不一致；任何必需事实缺失均断言整单 unavailable。

## 2. R2：服务侧逐候选 execution ledger

### 2.1 不改公共引擎 API

审查确认独立候选路径对买入阻塞仅执行 `_count(block_reason); continue`（`backend/app/backtest/engine.py:1068-1071`），结果 `execution` 也只是聚合统计（`engine.py:1955-1978`）。因此不扩展或改写公共 `BacktestEngine`/`simulate_independent_candidates` 签名，不要求引擎输出逐候选新字段。

### 2.2 ledger 契约

服务在每臂调用 engine **之前**生成逐候选 ledger；每行唯一键为 `(symbol, signal_date, arm)`：

```text
symbol, signal_date, segment, arm,
planned_execution_date, decision_price, anchor_value,
filter_retained, executable_precheck,
engine_entry_index, unfilled_reason, censoring_reason
```

`planned_execution_date` 来自 §3。服务使用 pinned execution-day bar 与 PIT facts 做可执行性预检：

- 无精确 T+1 bar：`executable_precheck=false, unfilled_reason=next_bar_missing`；
- T+1 为停牌/无交易 bar：`blocked` 或 `censored`，保留具体原因；
- T+1 一字涨停买入不可达：`unfilled_reason=buy_limit_up`；
- arm 主动过滤：`filter_retained=false, unfilled_reason=arm_filtered`，不得混入 engine 阻塞计数；
- 其余候选才进入该 arm 的 `entries`，并以 `engine_entry_index` 映射到 panel。

这是研究的“可执行候选集合”，不是未来函数：信号/mask 只读 T 以前数据；执行日 facts 仅用于事后记录实际可达性和 blocked/censored 结果。若 engine 返回的 aggregate block 计数与 ledger 不一致，视为内部契约错误并 fail-closed，不用聚合计数反推某一事件。

### 2.3 虚拟结局

none 臂 ledger 是唯一基准。被过滤事件按 `(symbol, signal_date)` one-to-one join none 臂 ledger：

- none 臂成交：复制该 candidate 的成本后净收益、exit reason、MAE/MFE，标记 `virtual_source=none_arm`；
- none 臂 blocked/censored：虚拟结局同样标为 blocked/censored，携带 ledger 的精确原因；
- 不存在 ledger 行或出现重复键：结果不可审计，整单 unavailable。

## 3. R3：按 pinned calendar 锁定 T+1

不得使用 `engine.py:815-820` 的相邻同 symbol 行 shift 作为日期定义。服务先从 pinned canonical reader 的 `market_days` 计算：

```text
planned_execution_date = first market_days[d] where d > signal_date
```

再检查该精确日期是否存在同 generation 的完整 OHLC bar，并检查 §1 的 PIT facts。缺 bar、停牌或执行资料不完整时，该事件在 ledger 中标记 `blocked/censored`，不把 T+1 后首个可用 bar 当作成交日，也不将该候选送入 engine。不存在下一市场日时记 `next_market_day_unavailable`。

同一 pinned calendar 还负责锚年龄、窗口计数、IS/OOS 归段；事件的 signal date、planned execution date、实际成交日期必须分开返回。引擎只接收已经锁定 index 的 executable entries，服务不得再依赖 engine 的隐式 shift。

## 4. R4：每臂 IS/OOS 分段响应

### 4.1 归段规则

按 `signal_date`（不是执行日或退出日）归段：`signal_date < oos_start → is`，`signal_date >= oos_start → oos`。理由是过滤决定在 T 收盘作出，归因必须对应决策信息集；事件仍返回 planned execution date 供审计。

### 4.2 arms 结构

v2 将 v1 的单一 `stats/layers` 替换为：

```json
{
  "arms": {
    "none": {
      "segments": {
        "is":  {"stats": {}, "layers": {}, "blocked": [], "censored": []},
        "oos": {"stats": {}, "layers": {}, "blocked": [], "censored": []}
      }
    },
    "original": {"segments": {"is": {}, "oos": {}}},
    "inverted": {"segments": {"is": {}, "oos": {}}},
    "random": {"segments": {"is": {}, "oos": {}}}
  }
}
```

四臂必须使用同一 signal universe；每段 stats/layers 使用相同 candidate-sample 白名单、分母和分层定义。事件 `segment` 与其所属 segment 统计必须一致；blocked/censored 计数不能从分母静默删除。

### 4.3 verdict 数据源

verdict 只读取 `arms.none.segments.oos.stats` 与 `arms.original.segments.oos.stats`。IS 不得参与 validated/rejected/inconclusive 判定，只用于诊断披露。OOS 样本不足（none 或 original 成交候选少于 30）为 `inconclusive`；达到门槛且 stop-hit 降低、净 expectancy 不恶化才可 `validated`，否则 `rejected`。

## 5. R5：candidate-sample 统计定义

### 5.1 移除的承诺

从 v1 stats 白名单删除 `turnover`、`cost_total`、组合 `max_drawdown`，也不返回任何 portfolio equity、initial-capital、组合换手或组合成本承诺。审查确认独立候选的“样本收益曲线”明示不是账户净值（`engine.py:1986-1988`），组合换手/成本统计位于组合统计路径（`engine.py:2143-2146`），不可冒用。

### 5.2 v2 白名单与分母

每个 arm、每个 `segments.is/oos` 只允许：

```text
n_signals                 # 该 segment 的共同 signal universe 数
n_retained                # arm filter_retained=true 数
n_filtered                # arm_filtered 数
n_candidates_executed     # ledger 通过可执行预检并送 engine 数
n_trades                  # engine 实际成交数
stop_hit_count            # exit_reason=stop_loss 的成交数
stop_hit_rate             # stop_hit_count / n_trades；n_trades=0 → null
win_rate                  # pnl_pct>0 的成交数 / n_trades
avg_win, avg_loss         # 成交样本正/负 pnl_pct 均值；无分母 → null
payoff_ratio              # avg_win / abs(avg_loss)；分母为零 → null
expectancy                # 成交样本 pnl_pct 均值
avg_mae, avg_mfe          # 成交样本、引擎成交口径的 MAE/MFE 均值
net_pnl_pct_mean          # 已扣双边费用/滑点/卖出印花的成交 pnl_pct 均值
exit_reason_counts        # 成交样本退出原因计数
blocked_counts            # ledger 未成交原因计数（按 reason）
censored_counts           # ledger/reader 删失原因计数（按 reason）
```

所有数值均明确 candidate-sample 分母；不输出账户级最大回撤、换手或成本总额。`net_pnl_pct_mean` 继承 engine 成交成本语义，但名称和说明明确是“成交候选样本均值”，不是组合回报。MAE/MFE 同样仅是成交候选的持仓质量诊断。

## 6. API v2 与 provenance

GET `/api/research/daily-open-anchor` 能力响应新增 `markets_facts` 能力：`requires_pre_close=true`、`requires_regime=true`、`requires_board_and_st=true`、`reconstructs_limit_down=true`、`intraday=unavailable`。若私有 reader 无法满足，GET 返回不可用原因。

POST `/api/research/factors/daily-open-anchor/evaluate` 的顶层结构沿用 v1，但 `arms.*` 必须采用 §4 的 `segments.is/oos`，并新增：

```json
"provenance": {
  "canonical_generation":"...",
  "markets_generation":"...",
  "manifest_sha256":"...",
  "oos_start":"...",
  "calendar_basis":"pinned_market_days",
  "limit_band_basis":"pre_close_plus_regime_st_board_round_half_up",
  "execution_ledger_version":1
}
```

响应必须包含 `execution_ledger` 或等价的逐事件 execution 状态字段；任何 `unfilled_reason` 都来自服务 ledger，不从 aggregate stats 猜测。顶层保持研究 disclaimer，无订单/方向/仓位字段。

## 7. 增量测试矩阵

在 v1 矩阵基础上新增：

1. `limit_band_facts` 返回缺 pre_close、缺 regime、缺 board/ST → 整单 unavailable。
2. main/ST/创业板/科创板/北交所比例和生效日期、Decimal ROUND_HALF_UP 边界正确。
3. 重建 upper 与 published ztj 不一致 → 整单 unavailable；lower flag 由重建下限生成。
4. PIT 市场日 T+1 无 canonical bar → ledger 精确标记 `next_bar_missing`，不延迟到下一可用 bar。
5. execution ledger 每 `(symbol,signal_date,arm)` 唯一；none blocked/censored 的虚拟结局原因逐事件准确。
6. engine aggregate 与 ledger 不一致 → fail-closed；公共 engine 签名和调用入口不变。
7. `segments.is/oos` 按 signal date 分段，事件 segment 与 stats 分段一致；IS 达标不能单独改变 verdict。
8. candidate-sample 分母断言：无 `turnover/cost_total/max_drawdown`；net pnl、MAE/MFE、stop-hit、blocked/censored 均可复算。
9. reader 不暴露 pre_close 的 fake reader → 能力 unavailable 或契约扩展测试失败，不允许降级。

## 8. 未修订条款与下一门禁

MA5/20 代理信号、收盘决策价、四臂定义、20 日锚年龄、symbols≤200、窗口≤370、前复权、非目标、免责声明和 sealed-only 红线仍按 [plan-v1.md](plan-v1.md) 生效。plan-v2 通过 review-v2 前不得进入代码实现；后续验证顺序仍为确定性夹具 → production reader 小样本冒烟 → focused tests → 全量 backend/Ruff → 独立 coding review。
