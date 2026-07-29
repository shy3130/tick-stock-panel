# 趋势监控逐分钟结果 ClickHouse 语义验收

状态：待实现与生产证据

适用需求：

- `REQ-DOW-MONITOR-MINUTE-RESULTS-SCHEMA-001`
- `REQ-DOW-MONITOR-MINUTE-RESULTS-MATERIALIZATION-001`
- `REQ-DOW-MONITOR-MINUTE-RESULTS-BACKFILL-001`

完成前必须记录：

1. DDL、永久保留、逻辑键与十四个指标列的直接 ClickHouse 证据；
2. A 股、港股、美股各一个分钟样本从原始历史到结果行的逐字段独立复算；
3. 因果时间、缺失值、数据质量、幂等补算和 ClickHouse 故障恢复证据；
4. 自动化测试计数、镜像 ID、回滚标签、运行状态、日志和监控股票文件哈希；
5. 与正式信号生成边界不变的验证结果。

在上述证据完成前，本文件不得标记为“通过”。
