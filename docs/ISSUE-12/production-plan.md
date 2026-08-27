# 生产接入方案（修订）

## 收窄目标

只接可证明的 price-event 证据。完整事件分类保持；缺 sortable tick 或历史盘口时必须 `bar_touched`/明确删失，禁止伪造封板、回封或一字板。

## 能力与所有权

- 拆分 `MINIMUM_CAPABILITIES` 与 `FULL_CAPABILITIES`。minimum：immutable manifest、sealed canonical daily、timestamped minute、PIT regime、PIT ST；production resolver 只接受 minimum。
- reader 实现完整 protocol；未声明的 tick/book/float 返回空/None。response manifest 返回 `FULL - actual`。只有相关事件分支追加对应删失。
- registry 测试 factory 默认 caller-owned；production reader 由 evaluator/API owned。resolver 返回所有权，成功/异常均 `finally` 恰好 close 一次，并级联关闭 provider/subreaders。

## 可证明输入

1. 日 K：`PublishedCanonicalDailyReader` raw OHLCV。
2. PIT 制度/ST/涨停价：新增 pinned markets PIT reader。`manifest.created_at` 是该 generation 全部行唯一可证明的 `available_at`；每个事实必须在其 effective day 的 **09:25 Asia/Shanghai 事件起点前**已可用，不能沿用当日 23:59:59 cutoff。任何早于真实 publication 的历史日期 PIT None，明确 `pit_first_available_at`；不把 trade_date 伪造成 available_at。每请求只固定当前 generation，无法证明旧 signal 时宁可 unavailable。PIT 同时返回 exact `ztj`，事件计算优先 exact limit price，regime pct 只作制度证据。
3. 分钟：#10 `PublishedOrderedTransMinuteReader` sparse true-trade 1m，coverage 外空。
4. 竞价：新增 provider-owned `PublishedCallAuctionReader(signal_year)`；构造时固定 exact generation/path/manifest hash，直接返回 `tick_index/event_time`。无唯一 preopen 最终记录则 None/`censored_preopen`；禁止调用每次跟随 current 且丢 tick_index 的旧 convenience method。
5. transactions 无稳定 seq、depth 无历史源：不声明 sortable tick/order book。float shares 首版不声明，禁止当前 zgb/ltgb 回填。

## Provenance

run manifest 扩展 `components`：canonical/markets/ordered-trans/callauction 各自 provider/route/generation/manifest hash/coverage/first-available boundary；canonical JSON 计算 composite SHA-256。响应原样保留 component map；任一 identity 变化 composite 必变。

## 真实验收

在 #10 coverage 内做 production reader smoke。由于 markets publication `available_at` 晚于旧 signal，历史请求允许且预期 PIT unavailable；若当日已发布 facts 且 cutoff 合法，manifest available。无 ticks/盘口时触板类不得产出 `sealed_limit/one_word_limit/broken_resealed`。测试覆盖 partial resolver、component manifest、exact ztj、available_at 双门禁、pinned callauction、sparse minute、所有权与无伪造分类。