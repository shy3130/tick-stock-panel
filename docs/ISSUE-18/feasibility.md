# ISSUE-18 可行性盘点（feasibility）

日期：2026-08-27 · 结论先行：**研究能力当前不可用，只能交付 fail-closed 契约 + 纯函数定义实现。**

## 1. 假设陈述

「单阳不破」：一根实体达标的**不复权（raw）**阳线出现后，后续固定窗口内
价格始终不跌破该阳线的最低价。研究目标是统计该形态的成立频率与窗口存活期，
为后续（远期）假设检验提供口径。**本期不含任何交易/回测执行语义。**

## 2. 数据面现状（代码事实）

| 事实 | 出处 | 对单阳研究的影响 |
|------|------|------------------|
| `kline_daily` 存 raw 不复权 OHLCV | `services/quote_service.py`（写侧注释「不复权原始价格」） | raw 四价理论上有源 |
| `kline_daily_enriched` 的 OHLC 为**前复权**，仅额外物化 `raw_close/raw_high/raw_low`，**无 `raw_open`** | `app/indicators/pipeline.py` 存储列注释（ENRICHED_COLUMNS 区段） | **raw 口径实体（open↔close）无法直接从 enriched 读出** |
| 前复权会改写历史价格形成除权缺口 | `pipeline._apply_adj_factor` 注释 | 用复权价判「破低点」会把除权缺口伪造成破位/不破位 |
| provider 层存在 fq=0 不复权备份日 K | `data_providers/fquant_provider.py`（fstore `day_klines`，fq=0） | 上游有 raw 数据，但**没有面向本研究的 reader 契约** |
| 回测域有数据快照 provenance 概念 | `app/backtest/provenance.py`（snapshot_hash、adjustment generation） | 该机制属回测域，**未抽象为通用 PIT reader** |

## 3. 缺口清单（为什么 unavailable）

1. **`pit_reader_missing`**：不存在「generation-pinned / point-in-time」的生产 reader
   为单阳研究供给 raw OHLC。enriched 缺 `raw_open`，用 `close/raw_close` 复权因子
   反推 `raw_open` 属于**伪造数据**，明确禁止。PIT 股票池同样无法证明
   （沿用 `provenance.py` 对幸存者偏差的既有警示口径）。
2. **`state_machine_not_implemented`**：信号生命周期（产生→窗口跟踪→确认/失效）
   没有状态机实现。没有状态机就没有「不破」的逐步确认事实，只能事后全窗口回看。
3. **`oos_not_implemented`**：无 IS/OOS 分离协议。回测域的严格 Walk-Forward
   属于另一条链路，本研究不能冒用它宣称样本外有效性。

## 4. 本期能交付什么

- **固定定义**（`final-design.md`）：把 raw 价、实体/影线、不破低点严格语义、
  窗口、T+1/OOS 约束一次性钉死为常量与文档。
- **纯函数实现**：`detect_single_yang` 仅接受显式传入的 bar 序列，无任何 IO。
  它的作用是**定义的可执行规格**（由单测锁定语义），不是生产信号源。
- **fail-closed 服务与端点**：`run_single_yang_research` 恒返回
  `status="unavailable"` + 上述三条 reasons + 定义回显；API 以 200 返回该载荷
  （沿用 `market_state.py` 「200 + state='unavailable'」与
  `attribution_report.py` `fama_french_unavailable_report` 的仓库先例）。
  **双保险**：即使将来 reader 补齐，只要状态机/OOS 未实现，仍必须 unavailable。

## 5. 明确不做（红线）

无交易语义（订单/持仓/仓位/止盈止损字段一律不出现）；无外部接口
（不连 HTTP/DB，不绕过 `data_providers` 抽象——本服务干脆零 IO）；
不改 `data/`；不动 `short_pool`；不接 Agent。
