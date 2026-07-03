# 0004 — 去档位后实时能力边界：watchlist 逐笔 vs 全市场快照

- **状态**：已接受（2026-07-02）
- **相关**：[ADR-0003](0003-waizao-supplementary-only.md)、数据源设计 Part B、`app/data_providers/fquant/fallback.py`

## 背景

- 去掉 TickFlow SDK 后，实时唯一本地源是 `tdx-api`，但它是**单标的** HTTP 接口（`GET /api/quote?code=`）。
- panel 现有两种实时模式：`watchlist`（少量标的）与 `full_market`（全市场几千只，原 Starter+ 付费档，`_fetch_full_market` 拉全市场→写 daily→算 enriched）。
- 量级矛盾：全市场每轮几千次单标的 HTTP 不可行。
- 同时正在删除 `tiers.yaml` 付费档位（full_market 实时本是付费档能力）。
- `fallback.py` 既有设计已把 `get_realtime` 兜底定为 `fstore:daily_markets`（批量快照）。

考虑过：
- **R2** 坚持全市场逐笔实时 → 需 tdx-api 提供批量端点（不在 panel 可控范围）。
- **R3** 砍掉全市场实时，只留 watchlist。

## 决策

**R1**：
- **watchlist 实时**：走 tdx-api 单标的（`watchlist.py` 已用线程池 chunk 并发），少量标的可提供接近逐笔的实时。`capabilities.realtime = true` 指此能力。
- **全市场"实时"**：重定义为 fstore `daily_markets` **批量快照语义**（近实时/快照，非逐笔），在 capability 与 UI 文档**诚实标注**其非 tick 性质。

## 后果

- ✅ 实时能力落地且不假装逐笔；与 `fallback.py` 既有意图一致。
- ✅ watchlist 场景（最常用）获得真实时。
- ⚠️ "realtime" 一词现承载两种语义（watchlist 逐笔 / 全市场快照），文档与 capability 必须区分，避免误解。
- ⚠️ 全市场逐笔实时（R2）若未来需要，是 tdx-api 侧加批量端点的独立后续项，不阻塞本设计。
