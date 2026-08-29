# 方案二次独立 review

结论：首轮的未来推断、尾部误判、artifact 映射和 interface 混用已关闭；calendar/coverage 仍有 2 个 blocker 与 1 个 major。

1. 只记录 market-day hash/count 不能让 reader 拒绝周末；必须持久化并校验完整 market-day artifact。
2. interval 未强制覆盖每个 pinned market day；源整日缺行必须表示为空 pool，不能变成 ledger gap。
3. `content_hash` 未冻结算法和 bytes；必须固定为 canonical JSON bytes 的 SHA-256。

处置：v2 已补 `logical=fstore.trade_date` 同代 calendar pin、`market_days.json`、首尾/相邻 market-day gapless 不变式、空 pool artifact，以及 canonical JSON raw bytes hash 规则。调整后再做最终冻结 review。