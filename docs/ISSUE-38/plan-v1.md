# ISSUE-38 方案 v1

## 目标与问题拆分

四个形态必须分别回答两个问题：

1. **形态筛选增量**：冻结条件命中的样本，相对各自父事件池是否有 OOS 成本后增量？
2. **持有规则增量**：同一命中样本上，“关键位破位退出”相对固定持有是否改善回撤且不过度牺牲收益？

不把四个形态合成一项统计，不把“控盘/洗盘/主力”写成事实，不在本 Issue 接入 short pool。

## 深模块与 interface

新增 `backend/app/services/hold_firm_patterns/` 深模块；外部 interface 只有：

```python
def assess_capability(reader, market_facts) -> CapabilityResult: ...
def evaluate_hold_firm_patterns(request, reader, market_facts) -> HoldFirmResponse: ...
```

API 只负责请求校验、构造请求级 pinned adapters、调用 interface 和关闭资源。检测器、状态机、执行、统计、删失和 provenance 都留在模块实现内部。

内部文件：

- `models.py`：冻结枚举、dataclass、Protocol、响应构造不变式。
- `first_yin.py`、`breakout_pullback.py`、`gentle_slope.py`、`platform_breakout.py`：四个纯检测器，只接收已排序 bars/PIT facts，返回父事件和命中 evidence，不做 I/O。
- `evaluation.py`：统一读取、执行、独立分母、IS/OOS、bootstrap/Wilson、诊断与 verdict。
- `__init__.py`：只导出两个外部 interface 与请求/响应模型。

不抽取现有 `n_shape` 私有函数，避免在本 Issue 改变既有因子；PIT 涨停算法按相同整数价格与 regime 口径在内部实现，并用交叉夹具锁定。

## 请求与数据门

`POST /api/research/factors/hold-firm-patterns/evaluate`

```text
symbols: 1..200 个规范化 A 股 symbol
start < oos_start <= end
cost_bps: 默认 10，范围 0..1000
固定 horizon: 20 market days
固定 forward checkpoints: 1/5/10/20 market days
extra fields: forbidden
```

整单 required facts：

- canonical identity：generation、manifest sha256、source generations/hashes；
- adjusted `open/high/low/close`、`volume`、`amount` 与 raw OHLC；
- pinned market calendar；
- canonical manifest 固定的 markets generation/manifest hash；
- 每个请求 symbol/day 的 PIT `regime/is_st/published bands/raw quotes/suspended/buyable/sellable`；
- PIT universe membership（没有可证明的历史 membership 时不得用当前股票池回填）。

canonical/markets identity、required columns、regime/is_st 或 source hash 不完整时整单 `unavailable`。单标的 warmup、bar 或 horizon 不完整进入 event-level censor ledger。

结构、均线与收益统一使用同 generation 的 adjusted 价格；raw 价格只用于涨跌停、停牌和实际执行证据。字段名显式区分 `research_*_adj` 与 `quote_*_raw`。

## 四个冻结基线

所有数值均是本研究自定，不归因于原视频；写入 `definition_version=v1` 与 `params_provenance`。

### F1 `first_yin_complement`

- 父事件：PIT 证明连续至少 3 个可交易涨停日；随后 1..5 个市场日内第一根 `close_adj < open_adj` 的已完成 K 线为首阴，之前不得已有阴线。
- 首阴结构：首阴 `close_adj >= MA5_adj`；MA5 由截至首阴日的 adjusted close 现场复算。
- 首阴量能状态：相对最后涨停日 volume，`<=0.70` 为 shrink，`>=1.50` 为 expand，中间态不满足量能条件。
- 评估日在首阴下一市场日收盘；互补要求：shrink 首阴后评估日 volume `>=1.50 × 首阴 volume`，expand 首阴后评估日 volume `<=0.70 × 首阴 volume`。
- 父池/递进对照：全部 3+ 连板后的首阴 → 首阴守 MA5 → 守 MA5 且两日互补；另报首阴破 MA5 组。
- 持有防守：评估日后 close 严格跌破 MA5，下一可卖时点退出；固定持有 20 日为同事件对照。
- 必报：首阴后连续跌停日数、无法卖出占比、量能方向两桶。

### F2 `breakout_pullback`

- 父事件：突破日前 20 个完整市场日为平台，`(max(high_adj)-min(low_adj))/min(low_adj) < 0.15`。
- 突破：`close_adj > prior_platform_high_adj` 且 volume `>=1.50 × prior_20d_mean_volume`。
- 回踩：突破后 1..5 日内第一日满足 `low_adj <= level × 1.01`、`close_adj >= level`、volume `<=0.70 × breakout_volume`；`level=prior_platform_high_adj`。
- “量能持续走低”仅作诊断：突破后到回踩日的 OLS `log(volume)` slope < 0，不进入 v1 命中条件。
- 父池/递进对照：全部平台收盘突破 → 放量突破 → 放量突破且缩量回踩。
- 持有防守：回踩确认后 close 严格跌破 level，下一可卖时点退出；固定持有 20 日对照。
- 假突破：确认后 5 个完整市场日内任一 close 严格回到 level 下方；窗口不足删失。

### F3 `low_gentle_slope`

- 低位：信号窗口开始前 120 日价格位置 `(close_adj-min(low_adj))/(max(high_adj)-min(low_adj)) <= 0.35`；分母非正 unavailable。
- 20 日缓坡窗口：阳线占比 `close_adj > open_adj` 至少 60%；每个交易日 close-to-close return 位于 `[-3%, +3%]`；`log(close_adj)` OLS slope > 0 且 R² >= 0.60。
- 缩量：`log(volume)` OLS slope < 0 且窗口最后 5 日均量 `<=0.80 ×` 最初 5 日均量。
- 信号：当日 close 严格创前 19 日新高。
- 父池/递进对照：低位缓坡新高 → 增加阳线占比 → 增加缩量条件。
- 持有防守：close 严格跌破现场复算 MA20，或出现“放量滞涨”——volume `>=1.50 ×` 前 20 日均量且 `abs(close/open-1) <=1%`；下一可卖时点退出。
- “控盘”只作为 `hypothesis_label`；并列披露 amount/volume 分位、零成交与换手代理作为流动性枯竭诊断，不进入 signal mask。

### F4 `bottom_platform_breakout`

- 底部：平台起点前 120 日价格位置 `<=0.35`，算法同 F3。
- 平台：突破日前 20 个完整日，振幅 `<15%`。
- 突破大阳：`close_adj > platform_high_adj`、`close/open-1 >=5%`、volume `>=1.50 × prior_20d_mean_volume`。
- 父池/递进对照：全部平台突破 → 底部平台突破 → 底部平台放量大阳突破。
- 持有防守：close 严格跌破突破日实体底 `min(open_adj, close_adj)`，下一可卖时点退出；固定持有 20 日对照。
- 强弱分层只作诊断：后续 1..5 日始终守实体底=`strong`；始终守突破日 close=`very_strong`；出现 close 跌破实体底=`broken`。层互斥优先级为 `broken > very_strong > strong > unclassified`。
- 假突破：后续 5 日任一 close 严格跌回 platform high 下方。

## 事件、执行与删失

- `event_id = factor_id + symbol + anchor_date + confirm_date`；同因子同标的持有/pending 期间不重叠开新事件，不跨因子去重。
- 信号在 confirm 日收盘确定；研究执行从下一精确市场日 raw open 开始。停牌、缺 bar、invalid open、一字涨停分别进入明确 censor，不能向后找买价。
- 卖出信号收盘确认；下一市场日起寻找第一可卖日。停牌/一字跌停形成 `pending_exit`，实际开盘跳空按 raw open；到 horizon 仍不可卖为 censored。
- adjusted 价格计算归一化收益、MAE/MFE；raw open 只作为实际报价映射。费用在进入/退出各扣 `cost_bps`，不假定触发线可成交。
- 父事件未命中后续条件不是数据删失，而是 denominator audit 的 `not_selected`；数据不完整才是 censor。

## 响应与统计

顶层 `status=ok|unavailable`，`definition_version=v1`，并返回：

- `factors`：恰好四项；每项含独立 `parent_events/qualified_events/segments/censored/denominator_audit`。
- `is` 与 `oos`：父池、命中池、未命中池、动态防守臂、固定持有臂的样本数、成本后 checkpoint return、terminal return、MAE/MFE、holding days、Wilson 胜率 CI、bootstrap 均值/差值 CI。
- `diagnostics`：F1 连续跌停/不可卖，F2/F4 假突破，F3 流动性枯竭，F4 强弱层；market regime 在无可信 pinned 来源时固定 `unavailable`，不得用请求 cohort 生成。
- `verdict`：每因子独立 `accepted|rejected|unavailable`，并拆为 `selection_verdict` 与 `holding_verdict`。OOS qualified complete segments <30 时为 unavailable；selection 仅当相对父池的 OOS 成本后 20 日均值差 95% bootstrap CI 下界 >0；holding 仅当动态臂相对固定持有的 OOS terminal-return 差 CI 下界 >=0 且 MAE 均值差 CI 上界 <0。总 verdict 只有两者都 accepted 才 accepted，任一 rejected 即 rejected，其余 unavailable。

不输出跨因子的合并胜率、总排序或交易动作。

## 验证矩阵

1. 四检测器：窗口边界、等点、阈值开闭区间、截断不变性、warmup。
2. F1：3/4 连板、首阴延迟、MA5 破/守、两种量能互补、中间量、连续跌停。
3. F2：平台边界、突破、缩量基准、回踩成功/失败、假突破。
4. F3：低位分母、阴阳比例 60%、±3%、OLS slope/R²、量能趋势、放量滞涨。
5. F4：底部、15%/5%/1.5x 边界、实体底、三级强弱、假突破。
6. 数据：canonical/markets pin 漂移、manifest hash、ST 5%、10/20/30cm、PIT universe、除权、停复牌、缺事实。
7. 执行：T+1、一字涨停不可入、一字跌停 pending、跳空、horizon、费用。
8. 统计：四分母隔离、IS/OOS 隔离、bootstrap/Wilson、样本不足与逐因子 verdict。
9. API：schema discriminator、extra forbid、reader lifecycle、unavailable 结构和无交易建议字段。

## 实施切片

1. 主会话冻结 `models.py` interface 与响应不变式。
2. 四个检测器按独立文件并行实现，只依赖 `models.py`。
3. evaluation/data adapter 与 API 可并行，严格消费冻结的 detector interface。
4. 集成后独立 coding review，修复后运行 focused/full suite，更新 Issue 与 PR。
