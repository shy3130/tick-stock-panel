# 方案 v1

## 1. 深模块与 seam

保留 `app.services.universe_scd` 作为 interval ledger 深模块：调用方只学习 contract、collection draft、原子发布和 pinned reader。将现有硬编码 `eligible_v1` 契约参数化，但默认值保持不变。

新增 `app.services.universe_presence_history` 作为 source adapter，只负责：

1. pin `fstore-markets` current generation、manifest hash、artifact size；
2. 只读查询 `daily_markets(asset_type=1)` 与冻结交易日；
3. 生成按 market day 排序、content-change 压缩后的完整历史 drafts；
4. 调用 ledger 的一次性原子历史发布接口。

## 2. 契约

新增不可变 `UniverseScdContract`：

- `schema_version`
- `artifact`
- `rule_version`
- `status_filter`

预置：

- `ELIGIBLE_V1_CONTRACT`：保持现有 schema/字段/行为；
- `PRESENCE_V1_CONTRACT`：独立 root，`rule_version=presence_v1`，`status_filter=daily_market_first_last_seen`。

`PublishedUniverseScdReader` 接受 expected contract，默认 eligible；manifest 不匹配立即 `UniverseScdIntegrityError`。

## 3. 历史生成算法

- 校验 frozen source 起止、日期非空、A 股 symbol 映射唯一。
- 每个 symbol 聚合 `first_seen/last_seen`。
- 用 frozen market-day 序列建立 add/remove 事件：first_seen 当日加入；非 source_end 的 last_seen 后一市场日移出。
- 扫描事件日生成 pool snapshot；相邻内容相同不重复建 interval。
- interval `effective_to` 为下一 snapshot 前一市场日；末 interval open。
- 每个 pool 单独 content hash/symbol artifact；整个 generation 一次写 staging、fsync、CAS current、原子替换。

## 4. fail-closed

- current/manifest/file symlink、size/hash、logical name、generation 格式任一异常拒绝；
- source coverage 早于 2022-03-04 或没有后继市场日时拒绝；
- code→symbol 非 A 股、日期越界、first>last、interval overlap/gap、规则混用拒绝；
- root 等于/位于 `DATA_DIR` 内拒绝；
- 发布失败保持旧 current。

## 5. 集成

`KlineRepository` 增加只读 `pit_presence_universe` 属性，返回 `PublishedUniverseScdReader(..., contract=PRESENCE_V1_CONTRACT)`；不改变 `pit_eligible_universe`。

Issue #38 后续只需把 production reader seam 显式切到 presence 属性并把 rule/source identity 写入 provenance，不新增行情 I/O。

## 6. 测试

- source pin/hash/size/symlink；
- 首次出现、末次出现、停牌空档、复牌、历史退市、source_end open；
- 上海/深圳/北京代码；
- 内容压缩、interval 边界、截断前 unavailable；
- contract mismatch、原子失败/current 不变、幂等；
- eligible_v1 全部既有测试不回归；
- repository 两个 universe 属性不混用。
