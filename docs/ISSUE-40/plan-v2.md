# 方案 v2

## 1. 语义：retrospective exact-day presence

`presence_v1` 只回答：在一个固定的 `fstore-markets` generation 中，某 symbol 是否存在 `asset_type=1 AND trade_date=event_date` 的 `daily_markets` 行。

- 不使用 first/last seen，不从未来行推断历史移出；
- 不把尾部 absence 解释为退市；
- 不跨日 carry-forward，停牌/源缺行当日为 `not_observed`；
- 仅允许用于回顾性研究 denominator，禁止作为 live/decision-time eligibility；
- manifest/provenance 明示 `retrospective=true`、source 发布时点和 coverage；Issue #38 的 membership 日必须是已完成 parent 的 anchor/landmark 日。

## 2. 独立 module/interface

新增 `app.services.universe_presence_history`：

- `publish_presence_history(root, data_dir) -> PublishOutcome`
- `PublishedPresenceUniverseReader(root, data_dir)`
- reader 暴露 `snapshot(day) -> PresenceDaySnapshot`、`presence_status(symbol, day) -> PRESENT | NOT_OBSERVED`、`prefetch_presence_days(days) -> Mapping[date, PresenceDaySnapshot]`、`source_manifest()`；snapshot 固定携带 `source_day_observed`、`symbol_count` 与 frozen symbols。它不暴露 `eligible_symbols` / `prefetch_event_days`，也不存在 `NOT_IN_POOL` 状态。

新增 `KlineRepository.pit_presence_universe`，不改 `pit_eligible_universe`。任何消费者必须显式使用 presence interface，并把 `rule_version=presence_v1` 写入 provenance。

## 3. Source pin 与 coverage

固定同一个 fstore snapshot manifest 及其两个 logical：

- `logical=markets`：查询 exact-day A 股 rows；
- `logical=fstore`：查询 `trade_date(mkt='A股', isopen=3)`，作为独立 pinned A 股 market calendar；真实源同日还包含港股与互联互通记录，必须按 `mkt` 过滤；
- 两个 artifact 均记录 generation、manifest SHA-256、file、size；manifest generation 必须相同；
- `coverage_start` / `coverage_end` 取 markets rows 边界，但覆盖内 market days 以 pinned `trade_date` 为准；
- `market_days.json` 保存完整、排序、唯一的 ISO market-day 列表；artifact bytes 为 canonical JSON，manifest 记录 SHA-256 与 count；reader 必须先验证该 artifact 并用集合拒绝周末/非市场日；
- source manifest `created_at` 进入 provenance；
- `(code, trade_date)` 重复、非 canonical A 股 code、任一 source/manifest/hash/size 不一致均整代拒绝。

coverage 外日期和非市场日 fail-closed；coverage 内若某市场日没有任何 markets row，则以空 pool artifact 明确表示 `not_observed`，不得偷换成 ledger gap。

## 4. Self-contained schema v2

独立 root：`TICKFLOW_UNIVERSE_PRESENCE_ROOT`，不与 eligible ledger 混写。

每个 content-change interval：

- `effective_from` / `effective_to` 均为 pinned market day；
- 第一段必须始于 `coverage_start`，末段必须止于 `coverage_end`；
- 相邻 interval 必须在 `market_days.json` 中首尾相接：后一段起点恰为前一段终点的下一 market day，禁止 gap/overlap；
- `content_hash = SHA-256(canonical_json_bytes(sorted_unique_symbols))`；
- `symbols_file = symbols/<content_hash>.json`，文件 bytes 必须正是上述 canonical JSON；
- `symbol_count`；空市场日使用 `[]` artifact，不能省略 interval。

所有 hash 去重后的 symbols artifacts、`market_days.json` 和完整 gapless interval ledger 都位于同一个 immutable generation。reader 对相对路径执行 no-follow 校验，逐文件验证 raw bytes hash、canonical JSON 重编码一致、排序、唯一性、canonical symbol 和 count，并用 market-day artifact 复核每个 interval 边界/邻接。schema/rule/status/retrospective 任一不符拒读。

## 5. 原子发布

- 构建到同 root staging；manifest、`market_days.json` 和全部 symbol artifacts fsync；
- 发布前 CAS `current.json`；
- generation digest 覆盖 manifest core（含 source、coverage、calendar identity、interval→artifact 映射）；
- generation 目录原子替换后再原子切 current；失败保持旧 current；
- 同 source generation/hash + 同 manifest core 为 idempotent。

## 6. Issue #38 后续集成

Issue #40 仅交付 presence publisher/reader/repository seam。Issue #38 在依赖合并后新增 `PinnedPresenceUniverseReader` adapter：只有 `PRESENT` 可进入 parent denominator；任一 parent membership 日为 `NOT_OBSERVED`、coverage 外或 source day 未观测时整单 `unavailable_universe_presence`，不得映射为 eligibility 的 `NOT_IN_POOL`。

## 7. 测试

- exact-day presence：存在、symbol 缺行、整日缺行、连续同 pool 压缩、pool 变化边界；断言 `PRESENT/NOT_OBSERVED` 不可混淆；
- coverage 前后、非市场日、source tail absence 均 unavailable/not_observed，不推断退市；
- source pin、重复 key、代码映射、calendar digest；
- symbols_file 路径穿越/symlink/hash/count/schema/contract；
- CAS、失败 current 不变、幂等；
- presence reader 不具备 eligible interface；eligible v1 全量既有测试不变；
- repository 双属性类型/规则隔离。
