# 趋势监控逐分钟结果 ClickHouse 规格

规格 ID：`USER-20260729-DOW-MONITOR-MINUTE-RESULTS-CLICKHOUSE`  
状态：权威  
批准：用户于 2026-07-29 明确确认逐分钟保存、永久保留、补算各市场今天历史并使用
WebSocket 原始离线历史。

## REQ-DOW-MONITOR-MINUTE-RESULTS-SCHEMA-001

3018 服务必须创建并使用
`longbridge.lb_dow_monitor_minute_results`。表必须采用
`ReplacingMergeTree(updated_at)`、按 `toYYYYMM(decision_minute)` 分区，并以
`(market, symbol, decision_minute)` 为排序键。表必须为当前页面的十四个监控指标、
基础行情、正式信号引用、数据质量和来源追踪提供独立可查询列或明确的追踪字段，并且不得
配置 TTL。

百分数字段必须保存百分数值，`1.25` 表示 `1.25%`；`volume_ratio` 和
`volume_speed` 必须保存倍数，`1.25` 表示 `1.25×`。缺失数值必须保存为
`NULL`，不得以当前值、估算值或零替代。

## REQ-DOW-MONITOR-MINUTE-RESULTS-MATERIALIZATION-001

对每只已启用监控股票和每根新完成的 1 分钟 K 线，3018 服务必须产生至多一个逻辑结果。
结果必须采用与趋势监控列表相同的单位、完成周期边界、15m→30m 稳定状态回退、缺失值
语义和正式信号边界。实时盘口和实时动能可以解释正式信号，但不得生成、清除、反转或
升级正式信号。

物化调度不得依赖浏览器或 WebSocket 客户端是否打开。ClickHouse 读取、建表或写入失败
必须与趋势状态、正式通知和 `/ws/realtime` 隔离，并且失败分钟必须能够通过持久化缺口
在后续周期重试。

## REQ-DOW-MONITOR-MINUTE-RESULTS-BACKFILL-001

3018 服务必须在启动后以及发现缺口时，按 A 股 `Asia/Shanghai`、港股
`Asia/Hong_Kong`、美股 `America/New_York` 的本地当前交易日，批量补算已启用股票
缺失的完整分钟。候选分钟只能来自该股票已有的完整原始 `min_1` K 线；不得为无原始
K 线的计划交易分钟合成结果。

每个连接值的来源时间必须小于或等于该结果的 `decision_minute`。原始历史缺失或超过
既有时效阈值时，相应结果列必须为 `NULL`，`data_quality` 必须为 `PARTIAL`，且
`missing_fields` 必须列出缺失字段。读取必须按股票集合和时间范围批量进行，禁止按
单只股票、单分钟循环查询 ClickHouse。今天以前的数据不得自动补算。
