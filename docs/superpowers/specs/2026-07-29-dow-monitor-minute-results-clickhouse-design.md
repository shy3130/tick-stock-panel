# 趋势监控逐分钟结果 ClickHouse 持久化设计

日期：2026-07-29  
状态：用户已确认架构、字段、补算与验收设计

## 1. 目标

趋势监控必须把每只已启用股票在每根完整 1 分钟 K 线结束时的监控结果持久化到
ClickHouse。结果用于分钟回放、信号发生前后的指标变化分析、组合有效性统计和后续
策略验证。

新结果表是可从原始历史和确定性计算规则重建的派生层。它不得替代或修改
`lb_realtime_quotes`、`lb_realtime_depth`、`lb_realtime_trades`、
`lb_realtime_candlesticks`、资金历史或正式通知历史。

本次范围包括：

- 永久保存上线后的逐分钟结果；
- 上线时补算 A 股、港股、美股各自当前交易日的历史结果；
- 保存页面当前使用的 14 个指标、基础行情、正式信号引用和数据质量；
- ClickHouse 短暂不可用时不阻塞监控，并在恢复后补写缺口；
- 提供生产查询和逐字段验收证据。

本次不增加历史回放 UI，不补算今天以前的数据，不改变正式信号生成规则。

## 2. 已确认的产品决定

1. 结果永久保留，不设置 TTL。
2. 补算范围仅为各市场本地“今天”的当前交易日。
3. 历史字段缺失时写 `NULL`，同时记录质量和缺失字段；禁止估算、补零或使用当前值
   倒填过去。
4. 原始 WebSocket 历史是补算权威来源。生产已经保存 quote、depth、trades、
   candlestick 和 capital；`brokers` 当前没有记录，但不是列表 14 个指标的必需输入。
5. 保存过程不依赖浏览器是否打开。

## 3. 权威需求

实施前在仓库规格体系中登记以下稳定需求：

- `REQ-DOW-MONITOR-MINUTE-RESULTS-SCHEMA-001`：建立永久、可查询、幂等的分钟结果表。
- `REQ-DOW-MONITOR-MINUTE-RESULTS-MATERIALIZATION-001`：每根完整分钟生成一次与监控页
  同语义的结果，写入失败不得影响监控或正式信号。
- `REQ-DOW-MONITOR-MINUTE-RESULTS-BACKFILL-001`：只使用因果可见数据补算各市场今天的
  完整分钟，缺失不伪造。

上述需求必须登记到 `docs/spec-index.yaml` 和 `docs/traceability.yaml`，分别关联生产
实现、可执行测试、语义验收和独立复核。

## 4. 架构

在 3018 TickFlow Stock Panel 后台增加独立的分钟结果物化边界。物化器复用现有
ClickHouse 连接配置、趋势监控市场时钟、指标定义和正式信号语义。

```text
ClickHouse 原始历史
  quote / depth / trades / min_1 candlestick / capital
                    │
                    ▼
按市场、标准股票代码、完整分钟做因果对齐
                    │
                    ▼
趋势监控分钟结果计算器
  基础行情 + 14 个指标 + 正式信号引用 + 数据质量
                    │
                    ▼
longbridge.lb_dow_monitor_minute_results
```

组件边界：

- `DowMonitorMinuteResultSource`：批量读取某批股票、某个时间区间的原始历史。
- `DowMonitorMinuteResultCalculator`：纯计算；输入因果切片，输出一分钟结果，不访问网络。
- `DowMonitorMinuteResultWriter`：建表、批量写入和读取已存在的分钟键。
- `DowMonitorMinuteResultMaterializer`：计算缺口、安排今天补算和实时增量，不参与指标公式。

这些边界必须支持依赖注入，以便测试真实的计算和缺口行为，而不是只测试 mock 调用次数。

## 5. 表结构

表名：

```text
longbridge.lb_dow_monitor_minute_results
```

表引擎：

```sql
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(decision_minute)
ORDER BY (market, symbol, decision_minute)
```

不配置 TTL。同一市场、标准代码和决策分钟重复计算时，以 `updated_at` 较新的版本为准。

### 5.1 标识和版本

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `market` | `LowCardinality(String)` | `cn`、`hk` 或 `us` |
| `symbol` | `LowCardinality(String)` | 存储标准代码，如 `1347.HK` |
| `display_symbol` | `String` | 页面代码，如 `01347.HK` |
| `decision_minute` | `DateTime64(3, 'Asia/Shanghai')` | 该完整分钟的决策时点 |
| `source_bar_time` | `DateTime64(3, 'Asia/Shanghai')` | 对应 1 分钟 K 线起点 |
| `calculation_version` | `String` | 指标计算版本 |
| `backfill` | `UInt8` | `1` 为历史补算，`0` 为实时增量 |
| `updated_at` | `DateTime64(3, 'Asia/Shanghai')` | 本次物化时间 |

`decision_minute` 使用 ClickHouse 的统一上海时区存储，但市场交易日和完整分钟判断必须使用
各市场时区。美股时间在写入前转换为同一绝对时刻，不得把纽约本地时间误标成上海时间。

### 5.2 基础行情

| 字段 | 类型 |
| --- | --- |
| `last_price` | `Nullable(Float64)` |
| `prev_close` | `Nullable(Float64)` |
| `change_pct` | `Nullable(Float64)` |
| `minute_open` | `Nullable(Float64)` |
| `minute_high` | `Nullable(Float64)` |
| `minute_low` | `Nullable(Float64)` |
| `minute_close` | `Nullable(Float64)` |
| `minute_volume` | `Nullable(Float64)` |
| `minute_turnover` | `Nullable(Float64)` |

### 5.3 十四个监控指标

| 分组 | 字段 | 类型 |
| --- | --- | --- |
| 趋势 / 位置 | `channel` | `Nullable(String)` |
| 趋势 / 位置 | `control_distance_pct` | `Nullable(Float64)` |
| 趋势 / 位置 | `vwap_distance_pct` | `Nullable(Float64)` |
| 动能 / 涨速 | `momentum_1m_pct` | `Nullable(Float64)` |
| 动能 / 涨速 | `momentum_5m_pct` | `Nullable(Float64)` |
| 动能 / 涨速 | `momentum_15m_pct` | `Nullable(Float64)` |
| 量价 / 资金 | `volume_ratio` | `Nullable(Float64)` |
| 量价 / 资金 | `volume_speed` | `Nullable(Float64)` |
| 量价 / 资金 | `active_buy_ratio` | `Nullable(Float64)` |
| 量价 / 资金 | `depth_imbalance_pct` | `Nullable(Float64)` |
| 突破 / 风险 | `distance_to_day_high_pct` | `Nullable(Float64)` |
| 突破 / 风险 | `distance_to_day_low_pct` | `Nullable(Float64)` |
| 突破 / 风险 | `atr14_pct` | `Nullable(Float64)` |
| 突破 / 风险 | `confirmation_count` | `Nullable(UInt8)` |

所有 `_pct` 字段保存百分数值，`1.25` 表示 `1.25%`。`volume_ratio` 和
`volume_speed` 保存倍数，`1.25` 表示 `1.25×`。`active_buy_ratio` 保存百分数值。
`confirmation_count` 的固定分母为 2。

### 5.4 正式信号

| 字段 | 类型 |
| --- | --- |
| `formal_signal_side` | `Nullable(LowCardinality(String))` |
| `formal_signal_stage` | `Nullable(LowCardinality(String))` |
| `formal_signal_label` | `Nullable(String)` |
| `formal_signal_time` | `Nullable(DateTime64(3, 'Asia/Shanghai'))` |
| `formal_signal_event_key` | `Nullable(String)` |

正式信号只能读取在 `decision_minute` 之前已经发生并持久化的通知或状态，不得由实时盘口、
实时动能或派生组合生成、翻转或升级。

### 5.5 质量和追踪

| 字段 | 类型 |
| --- | --- |
| `data_quality` | `LowCardinality(String)` |
| `missing_fields` | `Array(String)` |
| `source_timestamps` | `String` |
| `result_payload` | `String` |

`data_quality` 仅使用 `COMPLETE` 和 `PARTIAL`。`source_timestamps` 与
`result_payload` 保存 JSON，字段顺序不构成契约。查询频繁的字段必须使用独立列，不能只
藏在 JSON 中。

## 6. 指标因果语义

每个结果行以一根已经完成的 `min_1` K 线为基准。没有完整 1 分钟 K 线时不生成结果。

- quote：选择 `decision_minute` 前最后一份、且符合现有延迟阈值的报价。
- depth：选择该分钟内 `updated_at` 最晚的盘口快照；没有有效快照则盘口指标为 `NULL`。
- trades/capital：只使用 `decision_minute` 前已到达的数据；主动资金必须满足现有完整性
  和延迟规则。
- candlestick：只使用当时已完成的 1m、5m、15m、30m K 线。形成中周期不得参与稳定指标。
- VWAP：从截至该分钟的当日成交金额和成交量或权威分时均价计算。
- 正式信号：选择截至该分钟已经持久化的最近有效信号；没有则为 `NULL`。

计算公式、稳定周期回退、延迟门槛和缺失语义必须复用现有权威趋势监控规格。物化层不得
建立第二套近似公式。

## 7. 今天历史补算

补算入口按市场本地时钟确定当前交易日：

- A 股：`Asia/Shanghai`
- 港股：`Asia/Hong_Kong`
- 美股：`America/New_York`

对当前已启用股票：

1. 批量读取今天已有的完整 `min_1` K 线。
2. 批量读取覆盖同一区间的 quote、depth、trades、capital 和所需历史窗口。
3. 读取新表中已经存在的 `(market, symbol, decision_minute)`。
4. 仅计算缺失分钟；明确要求重算时允许写入新版本覆盖旧行。
5. 按市场和批次写入 ClickHouse。

补算股票即使当天较晚才加入监控，也补齐该股票今天原始数据能够支持的全部完整分钟。
今天以前的数据不自动补算。

禁止每只股票、每分钟分别查询 ClickHouse。原始数据必须按股票批次和时间区间读取，
计算在进程内按分钟顺序完成。

## 8. 实时增量

趋势监控仍按现有约 15 秒周期运行。物化器在监控周期完成后检查新表水位：

- 只在出现新的完整 1 分钟 K 线时产生结果；
- 每个股票分钟最多形成一个逻辑键；
- 重复周期允许幂等覆盖；
- 页面打开状态和浏览器 WebSocket 状态不参与调度。

实时写入和今天补算使用同一个 calculator，区别只体现在 `backfill` 标志和缺口范围。

## 9. 故障处理

- ClickHouse 读取或写入失败：记录物化状态和安全错误摘要，不影响趋势监控状态、
  正式通知或 `/ws/realtime`。
- 局部股票计算失败：其他股票继续；失败股票保留缺口供下一周期重试。
- 部分字段缺失：生成 `PARTIAL` 行，缺失列为 `NULL`，并写入 `missing_fields`。
- 缺少完整 1 分钟 K 线：不生成行，不用报价合成伪 K 线。
- 进程重启：通过新表最大分钟与原始 K 线重建缺口，不依赖内存游标。
- 建表：使用 `CREATE TABLE IF NOT EXISTS`，不得删除、清空或改写任何原始表。

状态接口应增加物化器的最近成功时间、最近错误、待补分钟数和最后写入行数，便于生产
排查，但不得把 ClickHouse 写入失败误报成正式信号失败。

## 10. 测试策略

### 10.1 下层语义测试

- A/港/美时区和交易日边界；
- 完整分钟判断；
- quote/depth/trades/capital 的 `as-of` 因果连接；
- 形成中 5m/15m/30m 不参与稳定指标；
- 14 个指标单位、正负方向、回退和缺失规则；
- 正式信号只读取当时已持久化信号。

### 10.2 存储行为测试

- 建表 DDL 无 TTL，键和引擎正确；
- JSONEachRow 序列化保留 `NULL`、数组、时间和版本；
- 批量插入；
- 重复分钟幂等；
- 已存在分钟不重复补算；
- ClickHouse 失败不阻断主监控；
- 恢复后能够发现并补写缺口。

### 10.3 规格合同

增加只位于 `tests/` 下的可执行规格合同，检查稳定 requirement ID、索引、追踪、实现、
测试和验收文件。不得用快照或 golden 代替语义接受。

## 11. 生产验收

发布时：

1. 备份当前 Compose 配置、容器 inspect、监控股票文件和回滚镜像。
2. 建立新表并确认没有 TTL。
3. 触发今天补算。
4. 分市场检查每只已启用股票的首分钟、末分钟、逻辑分钟数、`COMPLETE/PARTIAL` 分布和
   缺失字段。
5. A 股、港股、美股各选择一只，从原始表按因果时间重新计算样本分钟，与新表逐字段核对。
6. 验证新完整分钟持续增加，重复执行不会增加相同逻辑键数量。
7. 验证容器运行、重启次数、健康端点、错误日志和原监控股票数据不变。
8. 验证模拟 ClickHouse 写入失败时正式监控和信号仍正常，恢复后缺口补齐。

验收记录必须包含精确命令、实际记录数、样本计算过程、静态或镜像哈希、回滚信息以及
独立需求到证据复核。

## 12. 运维与查询

更新 Obsidian 运维手册：

`E:\Obsidian-alwin\alwin\longbridge-stock\dow-monitor-system-api-runbook.md`

记录新表、建表方式、补算命令、状态字段、常用查询、发布镜像、回滚点和验证步骤。

提供至少以下查询：

- 某只股票今天每分钟的 14 个指标和正式信号；
- 每只股票首末分钟及记录数；
- `PARTIAL` 行与高频缺失字段；
- 相同逻辑键的物理版本与 `FINAL` 去重结果；
- 最近一个完整分钟是否已经写入。

## 13. 非目标

- 不新增分钟回放页面；
- 不训练模型或自动评估收益；
- 不改变当前买卖信号规则；
- 不补算今天以前的数据；
- 不删除现有 JSON 状态和通知文件；
- 不把新结果表变成原始行情的权威来源。
