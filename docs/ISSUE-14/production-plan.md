# Forward eligible-universe SCD 生产方案（修订）

## 不可突破边界

只从首次真实采集后的**下一交易日**起建立 PIT universe。此前历史 as_of 必须 unavailable；不得由 current instruments/bar 覆盖/ssdate/base_infos_history 回填 available_at。

## Pinned source 与资格口径

collector 先解析并验证 fstore `current.json`、exact generation `manifest.json` 及其中 `logical=fstore` 文件，拒绝 raw fallback/symlink/path mismatch；用 exact published file 创建一次 read-only connection 查询，期间不调用 provider cache/current resolver。manifest 固定 source generation、manifest SHA-256、file identity。

eligible v1：canonical A 股 symbol、合法 `ssdate <= collection_date`。若 pinned schema 无可靠 status，manifest 写 `status_filter=unavailable`，不宣称退市/停牌过滤。

## 时间与 SCD

collection 固定 `available_at` UTC timestamp 和 versioned calendar identity；`effective_from` 为 collection 后的下一市场日，绝不以盘后 collection 覆盖同日事件。父 open interval 收口到新 effective_from 的前一市场日；缺采集日不造 snapshot，新集合不回溯，上一已知集合持续到下一真实 effective_from 前。

专用 root `TICKFLOW_UNIVERSE_SCD_ROOT`；对 root 与 `settings.data_dir` 均 `resolve()`，若 root 等于或位于 data_dir 任意子路径内即拒绝。parent expected-current + flock + fsync + atomic replace。同日相同 source/content 幂等，同日不同内容 conflict。

## 按事件日期的 reader seam

将 `PitEligibleUniverse` clean-cutover 为：

- `source_manifest()`：artifact/generation/source/calendar identity；
- `snapshot_identity(event_date)`：该日期唯一 interval 的 `content_hash/effective_from/effective_to/available_at`；
- `eligible_symbols(event_date)`：冻结集合。

`evaluate_volume_breakout` clean-cutover：只允许 `start <= event_date <= end` 进入事件扫描（扩展的前 60/后 40 日仅供 reference/整理/forward bars），并在扫描前一次性预取全部 request event market days 的 identity+symbols。任一 manifest/hash/path/unique-interval/read 错误，整次返回 `status=unavailable`，不降级成单事件 `pit_universe_unreadable`。事件写其 event_date identity hash；总 provenance 输出按 interval/hash 分组，不再用无参 snapshot hash/as_of。tests/fakes/callers 全部迁移，无兼容 alias。

## Repository 与 pipeline

repository lazy property 构造时完整验证 generation；失败不暴露 capability。daily pipeline 复用唯一现有盘后 hook，best-effort publish：失败不切 current、不阻塞其它步骤；calendar/source pin 与结果写日志。

## 验收

测试首日前/collection 同日 unavailable、下一市场日生效、两次 collection 收口、按事件 hash、同日冲突、parent CAS、path/data guard、完整性错误整 run unavailable。真实发布首个 snapshot 后只 smoke next-effective-date reader identity；历史 volume-breakout 继续 unavailable 是正确结果。