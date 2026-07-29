# 趋势监控逐分钟结果 ClickHouse 语义验收

状态：通过（2026-07-30，生产环境 `192.168.10.28`）

适用需求：

- `REQ-DOW-MONITOR-MINUTE-RESULTS-SCHEMA-001`
- `REQ-DOW-MONITOR-MINUTE-RESULTS-MATERIALIZATION-001`
- `REQ-DOW-MONITOR-MINUTE-RESULTS-BACKFILL-001`

## 1. 发布与运行证据

- 生产镜像：`tickflow-stock-panel-app:dow-monitor-bf2a5b993ffe`
- Git revision：`bf2a5b993ffe48fdf058f3e7ac4aa3a6b4fca2c2`
- Image ID：`sha256:0af853466c94ccd4d6ff92d599a39490b2acf0e07da4a60a1b46c52905abc73a`
- `/health` 返回 `{"status":"ok","version":"0.1.86","mode":"none"}`，容器重启次数为 0。
- 回滚容器：`TickFlow_Stock_Panel_release_backup_bf2a5b993ffe`，保留上一镜像 `dow-monitor-fa1317aa66c2`。
- 监控股票文件 SHA-256：`2d8da35aa9eb0da2faca894e72e1cd52e9518fad11a4844bc418962bfeb29ddb`，发布前后未变化。
- `/api/dow-monitor/symbols` SHA-256：`25bb010891a99802a4d7dc7fe226d85eed9b8f9a928e7263d32a69abe0bb3b5c`，发布前后未变化。
- 页面仍加载 `assets/index-VK-atCLA.js`；趋势监控、WebSocket 和正式通知边界未改动。

## 2. 表结构与永久保留

生产表为 `longbridge.lb_dow_monitor_minute_results`：

- 引擎：`ReplacingMergeTree(updated_at)`
- 分区：`toYYYYMM(decision_minute)`
- 逻辑键：`(market, symbol, decision_minute)`
- 无 TTL，分钟结果永久保留。
- 原始 OHLCV、涨跌幅、通道、控制线距离、VWAP 距离、1/5/15 分钟动能、量比、量速、主动买入比、盘口失衡、日高低距离、ATR14、确认数、正式信号、质量与来源时间均有直接列。
- 可缺失的数值与枚举使用 `Nullable`；`missing_fields` 明确记录缺口，禁止用 0 或占位值伪造。

## 3. 2026-07-29 历史补齐

使用原始完整 1 分钟 K 线驱动补算，生产 `FINAL` 视图结果：

| 市场 | 股票数 | 唯一分钟结果 |
|---|---:|---:|
| A 股 | 1 | 235 |
| 港股 | 5 | 1,536 |
| 美股 | 8 | 1,798 |
| 合计 | 14 | 3,569 |

港股展示代码（如 `01347.HK`、`00981.HK`）在查询原始数据时规范为 `1347.HK`、`981.HK`，避免前导零导致数据漏取。

物理表有 7,085 行（包含重试产生的旧版本行），逻辑键为 3,569 个；`FINAL` 为 3,569 行。`ReplacingMergeTree(updated_at)` 按逻辑键确定性保留最新版本，不产生逻辑重复。

## 4. 三市场独立复算

从 `lb_realtime_candlesticks FINAL` 以 `symbol + source_bar_time + period='min_1'` 连接结果表：

- A 股 `002714.SZ` 14:54：OHLC `38.81/38.81/38.81/38.81`、成交量 `361`，与原始行完全一致，1 分钟动能复算为 `0%`。
- 港股 `1347.HK` 16:01：OHLC `136.8/136.8/136.8/136.8`、成交量 `339000`，与原始行完全一致，1 分钟动能复算为 `0%`。
- 美股 `NVDA.US` 01:16：OHLC `192.93/193.03/192.78/192.87`、成交量 `150406`，与原始行完全一致；`(192.87-192.93)/192.93*100 = -0.031099362463070684%`，与结果列完全一致。

## 5. 因果、缺失与故障语义

- 对全部 3,569 行解析 `source_timestamps`，共检查 16,505 个来源时间戳；未来时间违规为 0。
- 全部历史行标记 `backfill=1`。当前上游缺少足够稳定 5/15/30 分钟证据时，相关通道、控制线、量速、ATR 或确认字段为 `NULL`，并标记 `PARTIAL`，没有伪造完整值。
- ClickHouse 建表或写入异常采用 fail-open：专项测试证明异常只写入 `minute_results.last_error`，不会中断趋势状态、正式通知或实时 WebSocket。
- 已存在分钟键时不构造历史上下文。生产首轮 `last_success_at` 更新且 `last_written_rows=0`，证明既有美股分钟键被直接跳过，不再每轮重放全天历史。

## 6. 自动化验证

- 分钟结果专项测试：28 passed。
- 发布包与不可变哈希测试：12 passed。
- 发布包规格检查：passed。
- 生产候选容器通过健康、前端入口、API、revision 和股票文件校验后才提升为正式容器。

结论：三项需求均具备实现、可执行测试与生产语义证据，验收通过。
