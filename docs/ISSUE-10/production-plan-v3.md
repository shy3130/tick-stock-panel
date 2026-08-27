# 生产接入方案 v3（最终候选）

## 数据面边界

runtime **不读取 raw CSV**。离线 publisher 是 upstream materialization 工具：读取 raw trans CSV、验证/保序并输出 published ordered-1m generation；FQuantProvider 只读该 generation 的 derived Parquet artifacts。

- 新 dedicated root：`FQUANT_SNAPSHOT_ROOT_ENGINE_A_ORDERED_TRANS`，默认 `/Volumes/WD1/duckdb/snapshots/engine-a-ordered-trans`。
- generation 目录包含 `manifest.json` 与 `bars/date=YYYY-MM-DD/<symbol>.parquet`；`current.json` 是唯一 runtime 路由。它与 extended/moneyflow/callauction 独立 root 同类，不进入业务 `data/`。
- `generation.py` 新增 logical owner `tdx_ordered_trans`；FQuantProvider factory 只能经该 published route 打开 reader，绝不接收 raw root。

## 离线 publisher

`backend/app/data_providers/fquant/ordered_trans.py` 实现纯 materializer，`backend/scripts/publish_ordered_trans.py` 只负责参数/调用。

- raw source 只由脚本显式传入；每个文件以 `O_NOFOLLOW` 单 descriptor 打开，`fstat` 普通文件/size 后一次读入 bytes；同一 bytes 完成 SHA-256、row count、header/parser 与解析。
- 仅接受实际六列 header 或七列加 `venue`；header 决定 parser variant，年份不参与。
- header 后物理行号为 0-based `source_seq`；重复 minute 不重排。仅保留正价格、正成交量连续竞价逐笔。
- raw `09:30..11:29/13:00..14:59` 加一分钟规范化为 close `09:31..11:30/13:01..15:00`；按 `(raw_minute,source_seq)` 生成 first/max/min/last/sum。
- 每 symbol/day 必须精确 240 根、timestamps 与两个 120 根 session 列表逐项相等；只有全部 requested symbols 都完整的日期进入 generation。
- 每个 symbol/day 写独立 Parquet，列固定 `symbol,ts,open,high,low,close,volume`。manifest entry 同时固定 source CSV relative path/header/parser/size/hash/rows 与 artifact relative path/size/hash/rows/first_close/last_close。

## 跨进程发布协议

1. 开始时读取 `current.json` 原始 bytes/hash 作为 expected-current；在 staging 之外完成所有 raw 读取、Parquet 写入、artifact hash 与 self-validation。
2. 取得 root `.publish.lock` 的跨进程排他锁；锁内重读 current 原始 bytes/hash，和 expected 不同即返回 conflict，保留完整未引用 generation 供审计，不更新 current。
3. 锁内 fsync 每个 artifact、manifest 与 generation 目录；写同目录临时 current，fsync 后 `os.replace`，再 fsync root 目录。
4. generation 名包含 UTC timestamp 与 manifest-content hash；已存在同名但 bytes 不同即冲突。发布器不自动删除既有 generation/current/raw。

## Runtime reader 与 active provider 生命周期

`PublishedOrderedTransMinuteReader` 固定 current generation/manifest bytes hash：

- 运行时只打开 manifest 固定的 per-day Parquet artifact；拒绝 symlink/path escape，单 descriptor 读取 bytes，核验 artifact size/hash 后从同一 bytes buffer 解码 Parquet。
- `catalog_manifest()` 返回 compact generation/parser/coverage/route identity；`manifest_sha256()` 单独进入 provenance；`market_days` 只返回 complete days；coverage 外 symbol/day unavailable。
- reader 再次逐项验证精确 240 timestamps 和 OHLCV；`sealed_cutoff=max_complete_day 15:00`；request-local cache；`close()` 清空。
- `ProviderCapabilities.ordered_trans_research`、schema 和 protocol factory `open_ordered_trans_reader()` 同步更新；仅 FQuantProvider true。
- API 通过 effective provider name `get_provider()` 获取**owned provider**；capability false/reader None 即 unavailable。`finally` 先关闭 owned reader、再关闭 owned provider。显式 registered test reader 优先且视为 caller-owned，API 不关闭它。
- service 不读 env/root，不持有全局 production reader；每请求重新固定 generation。

## 消费侧完整性

`ImmutableMinuteReader` 增加 `manifest_sha256()`；validator 不信任 adapter 声明：

- 每 symbol/day 要求恰好 240 根；每个 `bar.ts` 必须逐项等于 naive Asia/Shanghai close 列表 `09:31..11:30,13:01..15:00`；不得有 09:30、两分钟 gap、重复或额外 bar。
- OHLCV、symbol/day、strict monotonic、cutoff 均验证；失败统一 source-integrity unavailable。
- 5m anchors 必须为 `09:35...11:30,13:05...15:00`，15m anchors 必须为 `09:45...11:30,13:15...15:00`；聚合结果再次断言 anchors。

## 固定 split、标签、重叠与统计

- 请求新增必填 `oos_start`，`start < oos_start <= end`，provenance 固定 split。
- 对每个 signal/horizon：`raw_return = close[label_end] / close[signal_close] - 1`；`label_value=up/down/flat` 分别对应正/负/零。
- prediction hit：仅 prediction 为 up/down 且等于非-flat label 时 true；所有 flat prediction hit=false。
- `signed_return = raw_return * (+1 for up, -1 for down)`；flat prediction 为 null。`cost_bps=5.0` 固定，非-flat `post_cost_return=signed_return-0.0005`；MFE/MAE 继续按 prediction 方向和 label bar high/low 计算。
- 保存 `row_id/signal_close/label_end/horizon/label_value`；跨 `oos_start 00:00` 的 interval purge。
- 每 horizon 对所有 symbols 按 `(signal_close,symbol,row_id)` 做全局 interval purge；下一个 signal_close 必须严格晚于已保留 label_end。报告 raw/cross-boundary/overlap/effective 数。

## 基线与 verdict

在每个 horizon 同一 effective/common OOS row ids 比较：

1. unconditional：仅用 IS effective label 拟合多数 up/down；平票预测 flat。
2. momentum_5：signal close 对前第 5 根 15m close；正/负/零→up/down/flat。
3. sma5：signal close 对含当前的 5 根 SMA；正/负/零同上。

预热不足不进入 common set；flat prediction 的 hit=false、收益 null。factor/三基线分别输出 hit、Wilson 95% CI、mean signed/post-cost return、MFE/MAE。v1 参数不调优；IS 仅拟合 unconditional。`accepted` 仅当 horizon 1 common OOS ≥30、factor mean post-cost >0、factor Wilson lower >三基线最高 point hit rate；否则 `rejected` 并列原因。horizon 2 只诊断。

## 首个 bounded generation 与验收

固定 `600519.SH/000001.SZ/300750.SZ`，扫描 2026-07-01..2026-08-26，仅发布三者都完整的日期；`oos_start` 在发布后依据 complete days **预先冻结进 run 请求与验证文档**，运行前不看结果。coverage 外 fail-closed。

测试必须覆盖 publisher single-FD/header/source_seq/+1m/240/CAS，reader artifact hash/path/240/close，provider capability/所有权，service anchors/split/global purge/label formula/baselines/CI/verdict。真实发布后随机复核 source/artifact hash，运行 service/API 并记录 generation/hash/coverage/effective OOS/verdict；研究/provider/API 回归、ruff、独立 coding review 后交付。