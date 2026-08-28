# ISSUE-29 可行性评估：左一K线防守位
 
文档导航：[README.md](README.md) · [plan-v1.md](plan-v1.md) · [review-v1.md](review-v1.md) · [plan-v2.md](plan-v2.md)

## 结论

**可行，日线基线可立即落地。** 最小、可审计的 seam 是新增研究因子服务模块与两个 research API 端点，镜像既有 `single_yang_no_break` / `mtf_direction_15m5m` 模式；不修改生产回测引擎。服务内部建立持仓段状态机，逐条镜像引擎的 T+1、跳空成交和阻塞退出语义。

本评估不把原稿成功案例、减少卖飞、控制回撤或收益提升视为事实；真实结论须由实现后的 common-entry、IS/OOS 对照实验产生。

## 数据事实

### 日线 sealed canonical

评估基线的 `current.json` 指向已发布 generation `20260827T054651-63f500a4`，覆盖：

- 日期 `1990-12-19..2026-08-26`；
- 5679 个标的、8766 个交易日、约 1722 万行；
- `ENRICHED_STORAGE_COLS`：前复权 OHLC、`raw_*`、volume、amount、连续涨跌停字段。

深度、复权基准和 PIT 入口满足日线研究需要。唯一允许的数据通道是 generation-pinned reader；不得跟随计算期间变化的 `current.json`，不得拼接不同 generation，也不得直读 `data/`。

### 分钟数据为何不是 v1

唯一 sealed 分钟链路 `tdx_ordered_trans` 当前实测仅覆盖 **3 个标的 × 30 个交易日**（`2026-07-01..2026-08-20`）。因此 15m/1h 没有足够的统计深度，也无法形成可信 OOS；5 分钟聚合还要求窗口有成交，流动性不足必须 fail-closed。分钟全市场深回填前，不把它们列入本 Issue。

## 可复用能力

| 既有模块/符号 | 复用方式 |
|---|---|
| `services/canonical_history.py::resolve_published_history` | 发布 generation 与 manifest 校验 |
| `services/research_sealed_data.py::PublishedCanonicalDailyReader` | generation-pinned 日线 sealed reader；payload 输出 generation、manifest_sha256、columns、market_days、daily_bars provenance |
| `storage/repository.py:390-395` `generation_pinned_daily_reader` | API 从 repository 取得固定 reader |
| `services/single_yang_no_break.py` | `RESEARCH_ID`/`DEFINITION`、capability、fail-closed `unavailable`、censored、events/evidence、IS/OOS、成本字段形状 |
| `services/mtf_direction_15m5m.py` | common-OOS 对照、Wilson CI、显式 `accepted`/`rejected` verdict 范式 |
| `backtest/engine.py::_risk_exit` / `_can_sell` | 仅作语义参照：T+1、open≤防守线按实际 open 成交、停牌/一字跌停 pending；本 Issue 不改引擎 |
| `backtest/metrics.py` | 复用年化收益、Sharpe、最大回撤、交易持续期、bootstrap CI 等纯函数 |
| `indicators/pipeline.py` `atr_14` | Wilder EWM ATR14 口径；MA20/MA60 在模块内从 sealed bars 计算，避免非 PIT 存储列 |
| `services/research_registry.py` | Hypothesis/RunCard 与 reserved-tag 幂等治理 |

## 明确缺口

1. 现有 single-yang 是 forward-return 事件研究，不提供“分段持有 + 移动防守位 + pending exit”状态机；需在新服务模块内实现。
2. 现有代码没有从持仓段生成等权事件组合收益序列的 NAV/年化聚合层；需新增简单聚合逻辑并复用 `backtest/metrics.py`。
3. `BacktestConfig` 的风控线是百分比型，不支持由 bar 结构推导的防守线；本 Issue 不扩展引擎，validated 后另立 Issue 讨论。

## 非目标

- 15m/1h：因 sealed 分钟数据覆盖不足，无可信 OOS。
- 修改 `backtest/engine.py` 或把研究模块伪装成生产引擎替代品。
- 前端 UI 或前端重算；后端 payload 必须直接提供证据。
- 真实交易、下单、策略池、监控、生产调度。
- 写入 `data/` 或加入未经验证的默认策略。
- 为使结果达标而放宽冻结规则、无限调参或采信原稿收益主张。

## 风险与缓解

1. **状态机与引擎撮合语义漂移**：用跳空、T+1、停牌、一字跌停夹具钉死语义，并明确研究服务不改引擎。
2. **参数组合爆炸**：v1 只允许窗口、包含关系和破位后收回语义的冻结小集合；调参仅可在训练/验证段并记录。
3. **上涨状态过滤造成样本选择争议**：报告完整披露过滤前后事件分布、删失与 common entry set，不用筛选后样本伪装成全市场结果。
4. **复权与 generation 漂移**：单一 pinned generation 内以复权 OHLC 计算，raw 列只进证据；payload 固化 generation 与 manifest_sha256，缺数据/列时 fail-closed。

## 事实边界

本文件记录数据与架构可行性，不是回测结果。当前没有可引用的真实 OOS 收益、回撤或卖飞率；实施完成前不得在 README、API 示例或验收结论中伪造这些数字。