# 生产方案二审

结论：**v2 仍不通过。** 一审八项问题已在文字层面关闭，但实施前还有五项 P1：

1. runtime FQuantProvider 不能直接扫描 raw CSV；raw→OHLCV 必须是离线 upstream materialization，runtime 只打开 published generation artifact。
2. 每请求创建 active provider 时必须同时定义 provider 与 reader 的所有权/关闭；测试注册 reader 不属于 API，不应关闭。
3. current 发布必须有跨进程锁、expected-current 比较、fsync 与原子替换，不能把 `os.replace` 冒充 CAS。
4. service 消费端也必须逐 symbol/day 强制校验精确 240 根 close timestamps，不能只信 publisher/adapter。
5. 必须冻结 `label_value`、flat、hit、raw/signed/post-cost return 和固定成本公式。

v3 改为离线 publisher 生成 hash-pinned per-symbol/day Parquet artifact，runtime 不接触 raw；并补齐上述生命周期、CAS、消费侧和标签公式。