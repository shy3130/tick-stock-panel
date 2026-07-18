# TickFlow 全路径市场作用域设计

## 1. 目标与纠偏

市场不是独立入口，而是贯穿整个应用的数据作用域。用户在任意业务路径切换 A 股、港股或美股时：

- 当前 pathname 保持不变；
- 页面 API 请求、React Query 缓存、标的搜索、指标计算、策略结果和回测默认股票池全部切换到所选市场；
- 不允许港股或美股页面继续显示 A 股缓存；
- 某市场不具备对应业务语义时，显示明确的“不适用/暂不支持”，不得静默回退到 A 股。

本设计采用“全局市场上下文 + 同一路由市场化 API/缓存”。不采用仅前端过滤，因为聚合值仍会污染；不复制三套路由，因为页面会分叉且难以维护。

## 2. 全局市场状态

新增 `MarketScopeProvider`，合法值为 `cn | hk | us`，默认 `cn`。

- URL 查询参数 `market` 是当前页面的权威状态，例如 `/screener?market=hk`；
- 切换市场只替换当前 URL 的 `market`，保留 pathname 和其他查询参数；
- 最近选择写入 localStorage，新打开未带 `market` 的业务路径时恢复最近选择；
- 左侧全局切换器在所有业务页面固定显示 A股/港股/美股；
- 所有市场相关 React Query key 的末级必须包含 market，防止跨市场复用缓存。

市场与数据源是两个正交维度。数据源设置决定读取哪个 provider，市场作用域决定从该 provider 读取哪个市场；provider 若缺少当前市场数据，页面显示该市场不可用，不读取其他市场兜底。

## 3. 后端市场契约

市场相关 API 增加 `market=cn|hk|us`，为兼容旧调用默认 `cn`。后端使用共享校验与过滤函数：

- `market_for_symbol(symbol)`：根据标准代码识别市场；
- `filter_frame_by_market(df, market)`：按 `.SH/.SZ/.BJ`、`.HK`、`.US` 过滤；
- 缓存键使用 `(market, as_of, 其他参数)`；
- 响应顶层返回 `market` 与 `currency`，便于 UI 自检。

以下接口首批完成市场化：

- `/api/overview/market`
- `/api/screener/market-snapshot`
- `/api/screener/run`、`/cached`、`/run_all`
- `/api/kline/instruments/search`
- `/api/watchlist/enriched` 与行情列表
- `/api/index/list`、`/search` 与行情
- `/api/alerts`
- 回测与优化请求体中的默认 market

## 4. 三市场本地数据物化

当前 10.28 ClickHouse 已有三市场日线，但 TickFlow 本地 `kline_daily` 和 `kline_daily_enriched` 只有 A 股。必须先完成物化，页面才能读取真实港美股指标。

- 从 ClickHouse `lb_daily_bars` 按市场分批拉取复权日线；
- 继续写入现有按日期分区的 Parquet，symbol 后缀用于市场隔离，不复制目录树；
- enriched 计算覆盖三市场，A 股涨跌停字段只对 CN 计算，HK/US 置空；
- 每个市场独立计算 latest date，避免不同交易日历互相覆盖；
- 标的名称、币种和 lot size 来自 `lb_symbols`，保留已有 A 股中文名；
- 数据状态 API 返回三市场各自的标的数、行数、最早和最晚日期。

物化和 enriched 重建必须可重跑，失败市场不影响其他市场。部署前保留 Parquet 与镜像回滚点。

## 5. 路径行为矩阵

| 路径 | CN/HK/US 行为 |
| --- | --- |
| 看板 | 使用当前市场的日线/实时快照计算涨跌分布、广度、成交额、趋势与排行榜；指数区域使用各市场基准。 |
| 自选 | 只显示当前市场自选，搜索和新增限定当前市场，行情与 K 线仍读真实 symbol。 |
| 策略 | 只加载当前市场 enriched；策略缓存与运行请求包含 market。 |
| 回测/优化 | 默认股票池限定当前市场，费用、币种、lot size、交易日历使用对应 MarketRule；结果明确标记市场。 |
| 个股/财务 | 搜索限定当前市场；已选 symbol 与市场不一致时清空并提示重新选择。 |
| 监控 | 规则和触发记录按当前市场过滤；无 symbol 的系统事件单独归类。 |
| 复盘 | 调用当前市场 overview，报告元数据保存 market。 |
| 指数 | CN 使用沪深核心指数，HK 使用恒指系列，US 使用主要指数或可用 ETF 代理；只显示有数据标的。 |
| 概念/行业 | CN 使用现有扩展维度；HK/US 只使用真实可用分类，缺失时显示“当前数据源暂无分类”，不展示 CN 数据。 |
| 连板梯队 | 仅 CN 适用；HK/US 显示“该市场无 A 股涨跌停/连板规则”。 |
| 数据/设置 | 属于市场中立路径，展示三市场覆盖状态；全局市场切换器仍可见，但不改变配置对象。 |

## 6. 看板跨市场语义

通用指标包括上涨/平盘/下跌、涨跌分布、强弱、成交额、量能、均线与新高低。A 股专属的涨停、跌停、炸板、连板和板块标签在 HK/US 隐藏，并用以下通用指标替换：

- 港股：52 周新高/新低、日内振幅、成交额排行；
- 美股：52 周新高/新低、日内振幅、成交额排行。

指数/基准配置由 MarketRule 提供。若基准行情缺失，显示不可用，不复用上证指数。

## 7. 错误与降级

- 当前市场无本地 enriched：显示“该市场数据尚未物化”，并提供数据页链接；
- provider 不支持当前市场：显示 provider 名称和缺失市场；
- ClickHouse 不可用：保留最后成功数据但标记 stale，禁止冒充实时；
- 单市场计算失败：错误仅出现在该市场，其他市场可继续使用；
- URL 传入非法 market：归一为 `cn`，不抛前端白屏。

## 8. 测试与验收

1. 在 `/`、`/watchlist`、`/screener`、`/backtest` 切换市场，pathname 不变，URL market 更新。
2. 每个市场相关 query key 和请求都包含 market。
3. 后端相同 endpoint 分别请求 cn/hk/us，返回的 symbol 不跨市场。
4. 看板 HK/US 不出现上证指数、涨停或连板文案。
5. 策略 HK/US 返回真实 enriched 行，不能复用 CN 缓存。
6. 回测 HK 使用 HKD 与港股 lot size，美股使用 USD 与 1 股默认单位。
7. 连板梯队在 HK/US 显示不适用，而不是 A 股数据。
8. 10.28 本地 daily/enriched 均覆盖 cn/hk/us，数量与 ClickHouse 股票池基本一致。
9. 全量后端测试、前端测试、生产构建和 Playwright 三市场路径验收通过。

## 9. 实施分段与回滚

1. 全局市场上下文、URL、缓存键与通用后端过滤契约。
2. ClickHouse 三市场日线回填和 enriched 重建。
3. 看板、自选、策略、回测接入真实市场数据。
4. 个股、财务、监控、复盘、指数接入；A 股专属页面增加明确能力边界。
5. 10.28 部署、浏览器验收和数据交叉核对。

每段独立提交。部署前保留当前镜像 `three-market-global-20260718-1727` 和数据目录快照；失败时同时回滚镜像与 Parquet。
