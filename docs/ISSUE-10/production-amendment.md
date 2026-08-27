# 生产实现口径修订：稀疏 1m、完整 5m

真实 bounded publish 首次运行返回 `no all-symbol complete date`。根因不是缺文件，而是 A 股收盘集合竞价微结构：例如 `2026-07-01 sh600519` 的 `14:57..14:59` 只有 `buyorsell=8, volume=0` 的指示价，最终成交在 `15:00`；因此“仅正成交逐笔 + 每日精确 240 根 1m trade bar”在正常交易日也不可满足。把零成交指示价伪装为成交 OHLCV，或用 15:00 最终成交回填 14:58/14:59，都会弱化 true trade OHLCV/引入未来信息，禁止采用。

修订后的不可变契约：

1. 正成交逐笔仍按物理 `source_seq` 保序；连续竞价 raw minute 映射到 `+1m` close，边界成交 `11:30/15:00` 映射到同名 close 并与该 close 既有 ticks 按文件顺序合并。
2. artifact 保存 canonical 240 close 集合的**严格递增子集**，不得伪造空分钟；首个 close 必须 09:31、末个 15:00。
3. 完整性门禁从“240 个 1m trade bars”改为“48 个 canonical 5m 窗口均至少含一笔正成交”；每个 5m OHLCV 直接由该窗口内已发布、保序的真实 trade bars 聚合，anchors 精确 `09:35..11:30,13:05..15:00`。
4. service 消费端独立验证所有 sparse timestamps 属于 canonical 240 集合、strict monotonic、OHLCV，然后按 timestamp 所属 5m window 聚合并强制 exact 48 anchors；15m 再由每 3 根 5m 聚合并强制 exact 16 anchors。
5. manifest artifact `rows` 为实际 sparse 1m 数；新增 `five_minute_windows=48` 与 `missing_close_timestamps`。coverage complete day 只由全部 requested symbols 的 48-window 门禁决定。
6. 其它 sealed generation、same-FD/hash、CAS、split/purge/baseline/verdict 契约不变。

补充真实探针：少数文件存在相邻 raw minute 逆序（如 10:24 后又出现 10:23）。publisher 不按全局时间重排；只按 raw minute 分桶，并在每个桶内严格沿物理 `source_seq` 取 open/close，从而保留同分钟真实顺序。

真实探针：`2026-07-01` 三标的均有 48/48 五分钟窗口；sparse close 数分别为 600519=238、000001=239、300750=239。该修订保留 true trade OHLCV，明确不把集合竞价指示价当成交。