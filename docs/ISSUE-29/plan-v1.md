# ISSUE-29 v1 实施方案：左一K线防守位

> 本文是可审阅的冻结方案，不是实现结果；本波只创建文档，不改代码、不运行测试、lint、格式化或构建。
> 上位议题：[GitHub Issue #29](https://github.com/wf2311/fm-workbench/issues/29)；基线 `7bf2982`。
 
文档导航：[README.md](README.md) · [feasibility.md](feasibility.md) · [review-v1.md](review-v1.md) · [plan-v2.md](plan-v2.md)

## 1. 交付边界与代码落点

新增 `backend/app/services/zuoyi_defense.py`，承载纯研究逻辑、持仓段状态机和 `RESEARCH_ID=zuoyi_defense_v1` / `ZUOYI_DEFINITION`；修改 `backend/app/api/research.py` 接入 capability/evaluate 两个端点；新增纯函数与 API 定向测试。复用 sealed reader、指标纯函数和注册治理；不修改生产回测引擎。

## 2. API 契约（冻结）

### Capability

`GET /api/research/zuoyi-defense` 返回 capability、冻结的 `ZUOYI_DEFINITION`、数据可用性及 provenance。`generation_pinned_daily_reader` 或必需列缺失时仍返回 HTTP 200，业务状态为 `unavailable`，并给出机器可读 `reasons`；不得猜测、降级到非 sealed 源。

### Evaluate

`POST /api/research/factors/zuoyi-defense/evaluate` 使用 `pydantic` `extra="forbid"` 的请求体。`oos_start` 必填；明确的 symbol/date/参数范围由定义约束。API 通过 `getattr(repo, "generation_pinned_daily_reader", None)` 获取固定 reader，前端不重算。

成功 payload 至少包含：

- `events`：symbol、入场/中位线/左一索引、防守位、包含判定证据、信号时间、实际执行价、pending 原因；
- `censored`：分类、symbol、截断位置和原因；
- 六臂 `segments` 及成本后指标；
- `is` / `oos` 分段、样本量、置信区间和 `verdict`（`accepted` / `rejected` / `unavailable`）；
- `provenance`：generation、manifest_sha256、columns、market_days 与计算口径。

## 3. 数据与 PIT 契约（冻结）

- v1 仅读取单一 generation-pinned published canonical daily history；不跨 generation 合并，不跟随运行中变化的 `current.json`，不读取或写入 `data/`。
- 价格计算使用同一 generation 的前复权 `open/high/low/close`；`raw_*` 只进 evidence/provenance，不与复权价混算。
- `turnover_rate` 禁用（既有 pipeline 注释明确其不满足严格 PIT）；换手诊断只能用可审计的成交字段和明确公式计算。
- MA20、MA60、ATR14 由模块按已完成 sealed bars 重算；ATR14 使用 `indicators/pipeline.py` 的 Wilder EWM 口径。
- MA60 暖机不足（少于 60 根完成 bar）返回 `censored`（`warmup_insufficient`），不是默认填充或静默丢弃。

## 4. 算法语义（冻结）

### 上涨状态与中位线

截至已完成 bar `T`，上涨状态成立当且仅当 `close(T) > MA60(T)` 且 `MA20(T) >= MA60(T)`。中位线窗口默认 3 根，允许冻结集合 `{3,5}`；在截至 `T` 的窗口内取 `high` 最大值。高点同价时 v1 **取最新 bar**（取最旧为显式参数变体，不得隐式改变）。

### 左一K线与防守位

从中位线向左搜索最多 10 根，第一根“不被完全包含”的 bar 即左一K线。v1 strict 包含定义为：候选 A 被右侧 B 完全包含，当且仅当 `A.high <= B.high` 且 `A.low >= B.low`；边界等点也算包含。lenient 包含关系只能作为显式参数变体。上限内找不到左一K线时无防守位、无离场信号，不视为系统错误。

防守位是左一K线 `low`。入场后 `close(T) < 防守位` 才构成严格收盘破位；等于防守位只是触碰，不离场。破位发生在信号日 `T`，执行为下一可交易日 open。

### 执行、可卖性与重算

- 入场日不出场，满足 A 股 T+1。
- 下一可交易日开盘低于防守位时，以实际 `open` 成交并按实际价计亏，不使用防守位价回填。
- 停牌或一字跌停不可卖时进入 `pending_exit`，延后到首个可卖 open；若数据在此截断则右删失。
- v1 破位次日收回**不撤销**已确认信号；撤销属于显式参数变体，不得混入默认结论。
- 入场后 `close` 创新高时，以截至当日已完成 bars 重算防守位；永不引用未来或未完成 bar。

## 5. 六臂对照与统计契约（冻结）

六臂必须由同一套冻结入场规则生成同一 `common_entry_set`，离场规则不能反向改变入场样本：

1. buy-and-hold（右删失）；
2. ATR 吊灯：`max-high - k * ATR14`，`k ∈ {2,3}`，只在训练集选择；
3. MA20 持有；
4. MA60 持有；
5. 左一防守位；
6. 左一 + ATR 复合，取两条线中较高者。

每边成本 `cost_bps` 默认 10，成本后再聚合等权持仓段收益，并复用 `backtest/metrics.py` 的年化收益、年化 Sharpe、最大回撤、交易持续期和 bootstrap CI。`oos_start` 必填，参数选择只能发生在训练/验证数据，且逐次记录。

`verdict` 只由 OOS 决定：common OOS segments 达到预设最低样本量，且相对最佳基准无稳定增量时，明确返回 `rejected`；证据不足或数据不可用返回 `unavailable`；不得因原稿主张或 IS 优势标记 `accepted`。统计阈值、样本量和 CI 必须随 payload 固化，不能事后改写。

诊断指标固定输出定义：

- 卖飞率：离场后 `N ∈ {5,10,20}` 日 close 创离场时 rolling-max 新高的占比；
- 破位后下跌深度：未来 `M` 日 `min(close) / exit_price - 1`；
- 防守位距离的 ATR 分位；
- 基于成交字段明确定义的换手诊断，禁用 `turnover_rate`。

## 6. 参数与结果状态

| 参数/状态 | v1 约束 |
|---|---|
| 中位线窗口 | 默认 3；集合 `{3,5}` |
| 同高 tie-break | 默认最新；最旧只能是显式变体 |
| 包含关系 | strict 默认；lenient 显式变体 |
| 破位后收回 | 默认不撤销；撤销显式变体 |
| 左一搜索 | 最多 10 根 |
| 成本 | 每边 10 bps 默认 |
| warmup | `<60` 根完成 bar → `warmup_insufficient` censor |
| 无左一 | 无信号，不报错 |
| 数据/列/reader 缺失 | fail-closed `unavailable` + reasons |
| 未来数据/截断 | 右删失，禁止补值 |

## 7. 测试矩阵

实现波必须覆盖以下确定性夹具，并验证完整测试文件而非只跑单个预期失败用例：

- 同价高点取最新（及显式取最旧变体）；
- 完全包含与等点包含（strict / lenient）；
- 下影触碰但收盘收回：`low <= line` 且 `close >= line` 不离场；
- 收盘破位、次日收回：默认不撤销与显式撤销变体；
- 跳空穿越：`open < line` 时按 open 成交计亏；
- 震荡连损、多段再入场；
- 停复牌：pending 与数据截断 censor；
- 一字跌停无法离场：pending；
- 缺列与暖机不足：unavailable / censored；
- 除权日前后防守位连续性（前复权）；
- 截断不变性：截去 `T` 之后数据重新计算，`T` 时点防守位序列完全不变，作为无未来函数的机器证据。

测试形态参照 `backend/tests/test_single_yang_no_break.py` 与
`backend/tests/api/test_research_factor_evaluate_api.py`：纯函数使用 fake reader；API 使用
TestClient 并将 fake reader 挂到 `app.state.repo`。不得使用真实 `data/` 作为测试隐式输入。

## 8. 验证契约（实施波执行）

实施波完成后依次执行：定向服务/API 测试；真实 reader 小样本冒烟；后端全量测试；Ruff F/E9；独立 coding review。失败必须保留原始证据并阻断 verdict/集成，不以删测试、放宽规则或伪造回测结果通过。本波仅交付方案，未执行上述命令。

## 9. Provenance 与治理

每次 evaluate 固化 generation、manifest_sha256、列清单、市场日范围、参数、成本、IS/OOS 切分和状态机版本。研究假设与运行结果通过 `research_registry.py` 的 Hypothesis/RunCard（含 reserved-tag 幂等）登记；真实 OOS 结果未生成前，不写收益结论、不进入策略池或监控。

## 10. 非目标与红线

1h/15m、生产回测引擎改造、前端 UI、真实交易/下单、策略池、监控、生产调度和任何 `data/` 写入均不在本 Issue。所有路径必须遵守 sealed canonical、严格 PIT、fail-closed、A 股 T+1 与“下一可卖 open”语义；研究服务是可审计实验，不是交易执行系统。