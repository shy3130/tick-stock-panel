# TickFlow 三市场 ClickHouse 与共享策略接入设计

## 1. 背景与目标

在保留 TickFlow 现有 UI、策略页面、回测页面和分析能力的前提下，接入现有 `longbridge-stock` 项目的 ClickHouse 历史/实时数据与策略能力，并在第一阶段同时支持 A股、港股和美股。

首个端到端策略为“三市场结构突破/趋势策略”，覆盖长期箱体、直角三角形和对称三角形三类形态。实时扫描与历史回测必须复用同一份策略核心，避免策略逻辑分叉。

## 2. 设计原则

- TickFlow 负责 UI、交互、回测、参数优化和分析报告。
- `longbridge-stock` 继续负责 Longbridge 实时订阅与行情落库。
- ClickHouse 是行情、分钟K、策略运行和策略信号的权威数据源。
- 共享策略核心不访问数据库、文件或网络，只接收标准化行情与参数。
- 实时运行与历史回放使用相同策略版本、参数和市场规则。
- 所有可交易信号必须保存 `known_at_utc`，回测只能在信号首次可知之后成交。
- 新增表、配置和缓存均为增量能力，不修改现有 ClickHouse 表结构。
- 现有 TickFlow UI 视觉体系保持不变，只扩展市场、币种、策略证据和运行状态字段。

## 3. 总体架构

```text
Longbridge 实时订阅
        ↓
ClickHouse 行情库
        ↓
ClickHouse 数据适配层
        ├─ 历史日K/分钟K → TickFlow Parquet 缓存
        ├─ 最新行情 → TickFlow 实时看板
        └─ 策略输入 → 共享策略核心
                         ├─ Longbridge 适配器 → 实时信号落库
                         └─ TickFlow 适配器 → SignalMatrix → 回测/优化/分析
```

### 3.1 `longbridge-stock` 职责

- 维持现有 Longbridge 实时订阅，避免 TickFlow 建立第二套重复订阅。
- 维持现有 `lb_daily_bars`、`lb_realtime_quotes`、`lb_intraday_lines` 等表的写入。
- 承载可版本化的共享策略核心。
- 实时运行共享策略，将运行记录和信号写入标准表。

### 3.2 TickFlow 职责

- 增加 ClickHouse Provider，输出 TickFlow 标准 Polars 数据结构。
- 将历史行情增量物化为现有 Parquet 目录结构，复用指标与矩阵回测热路径。
- 读取 ClickHouse 最新快照并复用现有实时行情刷新和 SSE 通道。
- 通过共享策略适配器将标准信号转换为 `SignalMatrix`。
- 扩展回测结果和实时看板，展示市场、币种、阶段、形态、证据和失效位。

### 3.3 共享策略分发

共享策略核心保存在 `longbridge-stock`，构建为版本化 Python 包。TickFlow 开发环境可编辑安装该包，生产镜像安装固定版本构建产物。每次运行记录语义版本、代码哈希和参数哈希。

## 4. 标准行情契约

### 4.1 通用字段

所有数据适配器统一输出：

```text
market              cn / hk / us
symbol              内部唯一代码，如 000001.SZ / 1.HK / A.US
trade_date          市场本地交易日
event_time_utc      UTC 行情时间
open/high/low/close
volume              原始成交数量
amount              本币成交额，映射现有 turnover
currency            CNY / HKD / USD
adjusted            是否复权
source              数据来源
known_at_utc        该数据实际可用时间
```

比例字段统一使用小数，例如 `0.0366` 表示 `3.66%`。内部保留现有港股代码格式，显示层可按需补零。

历史策略默认使用同一复权口径的日K；实时展示使用原始价格。复权价格与原始价格不得在同一次回测中混用。

### 4.2 标准分钟K表

新增 `longbridge.lb_minute_bars`：

```text
market, symbol, frequency, bar_time_utc, trade_date_local,
open, high, low, close, volume, amount, currency,
source, known_at_utc, updated_at
```

现有 `lb_intraday_lines` 通过增量任务聚合为分钟 OHLCV。回测只读取已物化分钟K，不在回测热路径临时聚合数百万条快照。

## 5. 策略运行与信号契约

### 5.1 策略运行表

新增 `longbridge.lb_strategy_runs`：

```text
run_id, strategy_id, strategy_version, code_hash, parameter_hash,
parameters, mode, markets, start_time, end_time, status,
created_at, finished_at, error_message
```

`mode` 取值为 `realtime`、`backtest` 或 `scan`。

### 5.2 统一策略信号表

新增 `longbridge.lb_strategy_signals`：

```text
signal_id, run_id, strategy_id, strategy_version,
market, symbol, timeframe, pattern_type, phase, action,
signal_time_utc, trade_date_local, known_at_utc,
score, confidence, reference_price, invalidation_price, target_price,
evidence, metrics, created_at, updated_at
```

`action` 取值为 `WATCH`、`ENTRY`、`EXIT` 或 `INVALIDATE`。`signal_time_utc` 是信号对应行情时间，`known_at_utc` 是该结论首次真正可知的时间。

### 5.3 首个策略阶段映射

| 策略阶段 | TickFlow 行为 |
| --- | --- |
| `FORMING`、`SQUEEZE`、`BREAKOUT_WATCH` | 仅展示和观察 |
| `BREAKOUT_CONFIRMED` | 产生入场信号 |
| `RETEST_CONFIRMED` | 无持仓时入场，已有持仓时只更新评分 |
| `ACCELERATION` | 更新评分，首期不自动加仓 |
| `FAILED`、`INVALIDATED` | 产生退出信号 |

结构 `score` 只表示形态质量，不得展示为上涨概率。

## 6. 三市场交易规则

新增独立 `MarketRule` 层，至少包含：

```text
market, timezone, currency, trading_calendar, trading_sessions,
round_lot_policy, same_day_sell_allowed, price_limit_policy,
fee_model, benchmark
```

### 6.1 市场差异

- A股：上海时区、本币 CNY、当日买入不可卖出、通常 100 股一手、按板块/ST配置涨跌停。
- 港股：香港时区、本币 HKD、允许日内卖出、买入手数来自证券元数据、不套用 A股涨跌停。
- 美股：纽约时区、本币 USD、允许日内卖出、默认 1 股、不套用 A股涨跌停。
- 佣金、税费、平台费、监管费用和滑点由市场默认模板提供，并允许回测页面覆盖，不写死在策略内。

### 6.2 成交时点

- 日线策略在收盘后产生信号，默认在下一有效交易时段开盘成交。
- 盘中策略在当前分钟完成后产生信号，只能在下一分钟或下一可成交报价成交。
- 不允许使用产生信号的同一根K线收盘价成交。
- 停牌、无有效价格、手数不足、涨跌停、同日卖出限制、现金不足和敞口不足均形成明确阻塞原因。

### 6.3 三市场账本与汇总

首期采用三套独立账本：CN/CNY、HK/HKD、US/USD。每个市场独立计算收益、回撤、胜率和基准超额；总览按用户选择的基础币种使用每日可知汇率汇总。

首期不支持三市场之间自动调拨资金。汇率缺失时保留单市场结果，不生成虚假的组合净值。联合净值使用联合交易日历，休市市场沿用上一有效净值。

## 7. 共享策略与 TickFlow 适配

共享策略公开接口：

```python
class SharedStrategy:
    metadata
    parameter_schema
    def required_fields(self) -> frozenset[str]: ...
    def required_warmup_bars(self, params: dict) -> int: ...
    def generate_signals(self, market_data, params: dict, context) -> tuple: ...
```

TickFlow 适配器负责：

1. 从 `MarketDataMatrix` 读取并按证券拆分标准时间序列。
2. 调用共享策略核心。
3. 按 `known_at_utc` 将信号对齐到可成交时间。
4. 将 `ENTRY/EXIT`、评分、信号代码和参考价转换为 `SignalMatrix`。
5. 将形态、阶段、证据、失效位和策略版本附加到交易记录。

参数优化页面从 `parameter_schema` 自动生成控件。优化缓存键必须包含策略版本、参数哈希、市场、证券范围、回测区间、行情数据版本和交易规则版本。

## 8. UI 扩展

保留现有页面布局和视觉体系。

### 8.1 回测页面

新增市场选择、三市场初始资金、基础币种、费用模板、基准、交易时段、分钟精确成交、策略版本和参数配置。

结果新增：

- 三市场独立收益与组合汇总。
- 按市场、形态和阶段拆分表现。
- 首次可知时间、失效位、退出原因和证据快照。
- 停牌、涨跌停、手数和市场规则阻塞统计。

### 8.2 实时看板

新增市场标签、形态类型、阶段、结构评分、突破价、回踩位、失效位、更新时间、数据延迟、策略版本和证据详情。

## 9. 异常处理与安全

- ClickHouse 不可用时停止刷新，不用旧数据冒充实时数据。
- 单市场失败不阻塞其他市场，失败市场显示不可用。
- 行情超过延迟阈值时标记 `STALE` 并暂停产生新信号。
- 分钟K缺失时只有用户明确允许才降级为日K，并在报告中记录。
- 策略异常按市场和证券隔离；不完整运行不得标记成功。
- TickFlow 使用 ClickHouse 只读账户；只有策略运行服务可写新增信号表。
- 数据库凭证只来自环境变量或服务器密钥文件，不通过前端返回。
- 自定义策略不能访问系统命令、文件系统或网络。

实时状态至少展示数据最新时间、延迟、策略最后成功时间、各市场状态、失败原因和策略版本。

## 10. 测试与验收

测试分为数据契约、市场规则、策略因果性、回测一致性、集成/API/UI 五层。

首期验收必须满足：

1. A股、港股、美股均可加载历史日K和最新实时行情。
2. 标准分钟K可以增量生成。
3. 三市场交易日、时区、手数和费用规则正确。
4. 结构突破策略可同时扫描三市场。
5. 策略可在 TickFlow 回测并进行参数优化。
6. 结果分别展示 CNY、HKD、USD，并可生成统一基础币种汇总。
7. 历史回测不存在未来数据泄漏。
8. 相同策略版本、参数和数据下，实时重放与历史回测信号一致。
9. 数据过期、数据库故障和策略失败均有明确状态。
10. 不干扰现有实时订阅，不修改现有 ClickHouse 表结构。
11. TickFlow 现有后端测试和前端生产构建继续通过。

## 11. 实施顺序与回滚

实施顺序：

1. 三市场规则、数据契约和 ClickHouse Provider。
2. 分钟K物化与 Parquet 增量缓存。
3. 共享结构突破策略核心及因果性测试。
4. TickFlow 策略适配器和回测接入。
5. 参数优化、三市场报告和实时看板。
6. 10.28 部署、历史回放、性能验证和切换。

新增能力使用独立开关：

```text
CLICKHOUSE_PROVIDER_ENABLED
SHARED_STRATEGY_ENABLED
MULTI_MARKET_RULES_ENABLED
```

关闭开关后，TickFlow 回到原有数据源和策略路径，不删除新增表或缓存。
