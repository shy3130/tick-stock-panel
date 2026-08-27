# 生产接入方案 v1

## 目标

把 `mtf_direction_15m5m_v1` 从“仅 fake reader 可运行”推进为真实、可复现、可审计的分钟研究链。服务仍不直读原始文件；`data_providers/fquant` 负责把保留物理行序的 TDX trans CSV 约束为 immutable generation，再生成 true 1m OHLCV。

## 数据事实与取舍

- published `market_minutes` 只有单价/成交量，不能证明分钟 open/high/low/close。
- published trans DuckDB 的 `time` 大量重复，`num/venue` 不能提供全历史唯一顺序，禁止依赖 DuckDB 物理行序。
- raw trans CSV 的物理数据行序与现有 trans 导入顺序一致；同一分钟内必须按该顺序确定 first/last。
- generation 不复制体量巨大的 raw 文件；manifest 固定每个纳入文件的相对路径、size、SHA-256 与 parser/sequence 规则。读取时重新核验，源文件任何变化都会 fail-closed。因此 immutable 的是“manifest + hash 证明的字节集合”，不是 raw 目录本身。

## immutable manifest

新增 `backend/app/data_providers/fquant/ordered_trans.py`：

- `publish_ordered_trans_generation(source_root, snapshot_root, symbols, start, end)`：只枚举显式 symbol/date coverage；仅保留所有请求 symbol 都有文件的日期；读取 header 后按物理数据行计数；写 staging generation、`manifest.json`，最后原子更新 `current.json`。
- manifest schema v1：`generation`、`created_at`、`schema_version`、`parser_version=tdx_trans_csv_v1`、`timezone=Asia/Shanghai`、`source_root`、`source_sequence=physical_data_row_zero_based`、`coverage`、`entries`。每个 entry 固定 `symbol/day/relative_path/size_bytes/sha256/rows/header`。
- 路径必须位于 `source_root` 内，文件必须为普通非 symlink；header 只允许已知 2010–2025 六列或 2026 七列；重复 time 合法且不得排序。
- publisher 不写项目 `data/`，默认 snapshot root 为 `/Volumes/WD1/duckdb/snapshots/tickflow-ordered-trans`，支持 env 覆盖。

## request-scoped reader

`PublishedOrderedTransMinuteReader` 在构造时固定 `current.json` 指向的 generation 与 manifest bytes SHA-256：

- `catalog_manifest()` 只返回 compact identity/coverage，不把全量 entries 塞进 API；另提供 `manifest_sha256()`。
- `market_days()` 只返回 manifest complete days；coverage 外 symbol/day 抛明确错误。
- `minute_bars()` 每个文件首次读取前复核 size/SHA-256；以 header 后 0-based `source_seq` 顺序解析；过滤 A 股连续竞价 session，按 `(minute, source_seq)` 生成 1m `open=first/high=max/low=min/close=last/volume=sum`。零/负成交量、无效数值、未知 header、路径越界、hash/row count 不一致均 fail-closed。
- `session()` 固定 A 股 09:30–15:00；午休由 bar gap 保留，不跨段聚合。`sealed_cutoff()` 来自 manifest 最大 complete day 15:00。
- reader 仅在单次 API 请求内缓存已验证文件与 bars，请求结束 `close()` 清空；不全局缓存旧 generation。

## 服务契约收口

- `ImmutableMinuteReader` 新增 `manifest_sha256()` 与 `close()`；resolver 优先测试显式注册 reader，否则从默认/env snapshot root fresh 构造生产 reader。
- API 用 `try/finally` 关闭 request-scoped reader。
- reader contract 增加 manifest hash、route/parser/sequence identity；session continuity 要求每个 segment 内 1m timestamp 连续，午休只能出现 11:30→13:00 断点，bar 必须在 cutoff 内。
- 现有聚合保留 1m→完整 5m→完整 15m，不得按固定数量跨 gap 拼接。

## 研究统计补齐

- 信号只在确认 15m bar close 后生效；5m confirmation 只消费其 children。
- 每个 horizon 保存 `[signal_close, label_end]`；同 symbol 区间相交时按时间先后只保留首个，跨 IS/OOS boundary 的 label 删除。
- 分别报告 horizon 1/2 的方向命中、成本后 signed return、MFE/MAE、Wilson 95% CI、覆盖与删失。
- 固定比较：无条件方向概率、前 5 根 15m close 动量方向、close 相对 SMA5 方向；同一冻结样本逐行比较。
- `accepted` 仅在 OOS 非重叠有效样本不少于 30、horizon 1 成本后均值 > 0、命中率 Wilson 下界高于最佳固定基线命中率时成立；否则 `rejected`。不因真实结果调整门槛。

## 真实 bounded generation

首个 production generation 使用流动性高且跨 SH/SZ 的固定 3 标的 `600519.SH/000001.SZ/300750.SZ`，日期取 2026-07-01 至 2026-08-26 中三者文件均存在的 complete days。它只证明生产链与真实 OOS 可运行；coverage 外请求必须 unavailable，不宣称全市场能力。

## 文件与验证

- 新增 provider/publisher + 测试。
- 修改 `mtf_direction_15m5m.py`、研究 API、相关测试。
- 更新 Issue 文档、`AGENTS.md`、`FQUANT_INTEGRATION_PROGRESS.md`。
- 定向测试覆盖：manifest 原子发布/固定 generation/hash/path/header/source_seq、重复 time 的 open/close、午休、缺分钟、coverage 外、hash 变化、cutoff、请求 close、overlap/purge、三类基线、CI/verdict。
- 真实只读 smoke：发布 bounded generation，复核随机文件 SHA-256，API/service 实际运行并记录 generation/hash/coverage/verdict。