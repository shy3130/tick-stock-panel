# 方案首轮独立 review

结论：v1 不批准，5 项均为 blocker。

1. `last_seen` 由完整后验 source 推断历史移出，会把未来观察写入早期 PIT。
2. 源尾部缺行不能区分停牌、漏报和退市，禁止据 absence 推断退出。
3. 现有 schema v1 的 `source_generation -> symbols.json` 无法在一个 generation 内表达多个历史 pool。
4. manifest 缺 source coverage、market-day digest 和可复算边界。
5. 复用 `eligible_symbols` duck-typed seam 会让 presence 被静默当成 eligibility。

处置：废弃 first/last interval 推断。v2 改为“事件日 exact-row retrospective presence”，每个 membership 只读取同日事实，不使用未来日期；独立 schema v2、自包含 hash artifact、独立 reader interface，并明确禁止决策时/live eligibility 使用。