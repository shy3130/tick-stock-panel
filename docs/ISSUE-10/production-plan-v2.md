# 生产接入方案 v2

## 定稿目标

以 provider-layer ordered-trans adapter 把真实 TDX trans CSV 约束为 hash-pinned generation，按物理行序生成 true 1m OHLCV，再运行既有 5m/15m 引擎。service 不直读路径；coverage、时点、OOS、重叠和基线全部冻结，无法证明时 fail-closed。

## Manifest 与发布

`backend/app/data_providers/fquant/ordered_trans.py` 提供 publisher 与 request-scoped reader。

- manifest schema v1 固定 `generation/created_at/schema_version/parser_version/timezone/source_root/source_sequence/coverage/entries`。
- `source_sequence=physical_data_row_zero_based`：header 后第一行 seq=0；重复 `time` 合法，禁止按 `time/num/venue` 重排。
- parser 仅按**实际 header**选择：精确六列 `time,price,vol,num,amount,buyorsell` 或精确七列加 `venue`；header→parser variant 写入每个 entry。年份不参与 parser 判断。
- publisher 对显式 symbols 与日期目录求交集。每个候选 symbol/day 必须从 raw 起始标签 `09:30..11:29`、`13:00..14:59` 规范化得到 close_time `09:31..11:30`、`13:01..15:00`，精确 240 根、无重复/缺失/额外；只有全部 symbols 都完整的日期进入 `complete_days`，且只为这些日期写 entries。
- entry 固定 `symbol/day/relative_path/size_bytes/sha256/source_rows/header/parser_variant/minute_bars=240/first_close/last_close`。路径必须位于 root 内且输入为普通非 symlink 文件。
- staging generation 完成、自校验后写 manifest，最后以 CAS/原子替换更新 `current.json`；发布不复制 raw 字节，后续任何源变化由 hash 门禁拒绝。

## 单描述符读取与 bar 语义

- reader 解析 `current.json` 后固定 generation 与 manifest bytes SHA-256，不跟随后续 current。
- 每个 raw 文件用 `O_NOFOLLOW`（平台支持时）单次打开；`fstat` 验证普通文件/size，**同一描述符读取到单一 bytes buffer**，在该 buffer 上完成 SHA-256、source row count、header/parser 与 CSV 解析。校验全部通过前不交付 bars。
- 只保留正价格、正成交量的连续竞价逐笔；以 `(raw_minute, source_seq)` 生成 1m OHLCV：first/max/min/last/sum。raw minute 加一分钟成为 bar close，输出严格 240 根。
- 5m close 固定 `09:35,09:40...11:30,13:05...15:00`；15m close 固定 `09:45...11:30,13:15...15:00`。午休只允许 `11:30→13:01` 的 1m gap，绝不跨段聚合。
- manifest entry、path、fstat、hash、row count、header、240-bar completeness 任一不一致即抛 source-integrity 错误；coverage 外 symbol/day 明确 unavailable。
- `catalog_manifest()` 返回 compact generation/parser/coverage/route identity；`manifest_sha256()` 单独进入 provenance；`sealed_cutoff` 为最大 complete day 15:00（Asia/Shanghai 语义，代码统一 naive local datetime）。`close()` 清空 request-local 验证/bar cache。

## Active provider 接入

- `ProviderCapabilities` 新增 `ordered_trans_research`，同步 schemas 与所有 provider 实例；当前仅 fquant/fquant_local 实现为 true。
- `MarketDataProvider` 增加 `open_ordered_trans_reader()` factory；FQuantProvider 从 snapshot root/env 构造 fresh reader，manifest 不存在返回 None。
- API 通过 active provider + capability 获取 reader；service resolver 只校验协议，不自行读 env/root。测试显式 registered reader 仍可优先注入。
- `ImmutableMinuteReader` 增加 `manifest_sha256()` 与 `close()`；API `finally` 关闭 owned reader。每次请求重新固定 generation，首次 unavailable 或旧 generation 不缓存。

## 固定 OOS 与无前视标签

- `MTFDirectionEvaluateIn` 新增必填 `oos_start: date`，必须满足 `start < oos_start <= end`；provenance 记录同一 split。
- signal 仅在 confirmed 15m close 生效；horizon 1/2 保存 `row_id/signal_close/label_end/label_value`。任何 `[signal_close,label_end]` 跨 `oos_start 00:00` 的 IS/OOS 边界即 purge。
- 每个 horizon 分别把所有 symbol rows 按 `(signal_close,symbol,row_id)` 排序，执行**全局** interval purge：下一个 `signal_close` 必须严格晚于已保留 `label_end`；同时发生时按 symbol 字典序只保留一个。报告 raw rows、cross-boundary purged、overlap purged、effective rows。
- 只有 effective rows 用于样本量、Wilson CI 和 verdict；不得对重叠 raw rows做独立 Bernoulli 推断。

## 冻结基线与 Verdict

所有基线在 signal close 时产生预测，并在每个 horizon 的同一 effective row ids 上比较：

1. `unconditional_is_majority`：仅用 IS effective rows 的真实 label sign 拟合一个常量方向；up/down 数相等则预测 flat，flat 计未命中，不读取 OOS 分布。
2. `momentum_5`：`close_t` 对 `close_{t-5}`；大于为 up、小于为 down、相等为 flat；不足 5 根时该 row 不进入 common comparison set。
3. `sma5`：`close_t` 对 signal close 前含当前的 5 根 SMA；大于/小于/相等规则同上；不足时不进入 common set。

- factor 与三基线最终只在三者均可用的 common OOS effective rows 上计算；同时报告 common-set 丢失数。
- 每个 horizon 输出 hit rate、Wilson 95% CI、mean signed/raw/post-cost return、MFE/MAE；flat prediction 的 hit=false、signed/post-cost return=null。
- v1 参数完全冻结，不做数据驱动调参；IS 仅拟合 unconditional 常量。`accepted` 仅当 horizon 1 common OOS rows ≥30、factor mean post-cost return >0、factor Wilson 下界高于三类基线最高 point hit rate；否则 `rejected` 并列出原因。horizon 2 只作独立诊断，不替代主门槛。

## 首个真实 bounded generation

固定 `600519.SH/000001.SZ/300750.SZ`，扫描 2026-07-01..2026-08-26，实际 header 选择 parser，仅发布三者均满足 240-bar completeness 的日期。真实研究请求以 complete coverage 前半段为 IS、预先写死的 `oos_start` 为 holdout；coverage 外请求 unavailable。该 generation 只证明生产链和真实 verdict 可运行，不宣称全市场能力。

## 验证与交付

- manifest/publisher：header variants、source_seq、同描述符 hash、path/symlink、原子 current、完整日交集、源变化。
- reader/bar：重复 minute 的 first/last、+1m close label、240 bars、午休、5m/15m close anchors、coverage/hash/cutoff/close。
- service/statistics：required split、cross-boundary/global overlap purge、三基线无前视/common rows、Wilson/verdict、无交易语义。
- 真实发布后随机复核 entry SHA-256/rows；运行 service/API，记录 generation/hash/coverage/effective OOS rows/verdict；研究域/provider/API 相关回归、ruff 与独立 coding review 后才提交 PR。