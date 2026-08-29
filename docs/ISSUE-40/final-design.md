# 最终设计（冻结）

冻结基线：[plan-v2.md](plan-v2.md)。实现不得偏离以下不变式：

1. `presence_v1` 是 pinned generation 的 retrospective exact-day row presence，不是 `eligible_v1`，不用于 live/decision-time eligibility。
2. 同代 pin `logical=markets` 与 `logical=fstore`；前者供 rows，后者以 `trade_date(mkt='A股', isopen=3)` 供完整 A 股市场日历（真实源同日含港股/互联互通多条记录，禁止跨市场混合）。
3. schema v2 generation 自包含 `market_days.json`、gapless intervals 与 `symbols/<sha256>.json`；bytes/hash/canonical JSON/path/count 均可验证。
4. coverage 内整日无 rows 用空 artifact 表示，不得生成 ledger gap；coverage 外与非市场日 fail-closed。
5. reader 只暴露 presence interface：`PresenceDaySnapshot`、`PresenceStatus.PRESENT | NOT_OBSERVED`；禁止 eligible duck typing 和 `NOT_IN_POOL`。
6. publisher staging + fsync + current CAS + atomic switch；失败保持旧 current，同 source/core 幂等。
7. Issue #40 只交付 publisher/reader/repository seam；Issue #38 在依赖合并后单独接入，并对任一 `NOT_OBSERVED` 整单 unavailable。

任何需要 first/last seen、absence→退市、当前股票池回填、K线弱化段或 presence/eligible 混用的变更，必须先更新 Issue 契约并重新设计 review。