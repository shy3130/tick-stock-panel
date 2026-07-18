# Longbridge 财务数据接入设计

## 背景与目标

TickFlow 财务页已经具备财务指标、利润表、资产负债表、现金流量表四类展示和 AI 分析能力，但 10.28 当前仍选择 TickFlow 官方财务源，且没有 Expert Key，因此接口返回 `available=false`。

现有量化项目的 PostgreSQL `longbridge_stock.lb_financial_report` 已保存约 497 万条 A 股、港股和美股财务明细。目标是在不改变现有 UI 和 API 的前提下，把这批数据接入 TickFlow，并提供自动增量刷新。

## 方案选择

### 采用方案：TickFlow 内置只读 PostgreSQL 财务插件

新增 `longbridge_financial` 内置插件。插件通过专用只读 PostgreSQL 账号访问 `lb_financial_report`，按股票批量读取每个字段的最新报告期，再转换为 TickFlow 既有的四类标准记录。

选择理由：

- 不复制 11 GB 原始财务表，不增加 ClickHouse 重复存储。
- 不修改当前改动较多的 `longbridge-stock` API 文件，降低合并冲突和部署风险。
- 复用 TickFlow 已有的插件发现、财务同步、Parquet 缓存和前端展示链路。
- 数据库账号只授予单表 `SELECT` 权限，权限边界清晰。

### 未采用方案

1. 将 `lb_financial_report` 全量复制到 ClickHouse：查询方便，但会产生大规模重复数据和额外同步维护。
2. 在 Longbridge API 增加财务批量接口：边界清晰，但需要修改当前存在大量未提交改动的 API 文件，并增加另一套接口部署与监控。
3. TickFlow 通过宿主机命令或 Unix Socket 读取 PostgreSQL：容器耦合强，不利于测试和迁移。

## 组件与边界

### PostgreSQL 访问桥接

`backend/app/plugins/longbridge_financial/bridge.py` 只负责：

- 从 `LONGBRIDGE_FINANCIAL_DSN` 读取连接配置。
- 使用参数化 SQL 执行只读查询。
- 提供插件可用性检查。
- 不记录 DSN 和密码。

### 财务数据转换器

`backend/app/plugins/longbridge_financial/provider.py` 只负责：

- 分批查询股票，避免超长参数和数据库瞬时压力。
- 利用 `(symbol, field, fp_end, updated_at)` 索引取得各字段最新记录。
- 以股票最新 `fp_end` 为准，避免把不同报告期字段拼成一张表。
- 把字段级明细转换为四类 TickFlow 标准记录。
- 在一次“全部同步”期间缓存四类转换结果，避免四次重复扫描数据库。

### 能力门控

扩展现有自定义数据源能力补充逻辑：当财务数据源选择 `longbridge_financial` 且插件可用时，授予运行时 `financial` 能力。这样财务页、个股信息栏和后端财务接口都使用同一能力判断，不依赖 TickFlow Expert Key。

### 自动刷新

保留现有手动“全部同步”和单表同步。新增环境变量控制的自动同步：

- `FINANCIAL_AUTO_SYNC_ENABLED=true` 时启用。
- 启动后延迟执行一次，之后按固定间隔同步四类最新财务数据。
- 10.28 默认每天同步一次。
- 同步仍使用现有互斥锁，避免自动任务与手动任务并发。

## 字段映射

### 财务指标 `metrics`

| TickFlow 字段 | Longbridge 字段或计算方式 |
|---|---|
| `eps_basic` | `EPS.value` |
| `bps` | `BPS.value` |
| `roe` | `ROE.value` |
| `gross_margin` | `GrossMgn.value` |
| `net_margin` | `NetProfitMargin.value` |
| `debt_to_asset_ratio` | `TotalLiability / TotalAssets * 100` |
| `revenue_yoy` | `OperatingRevenue.yoy` |
| `net_income_yoy` | `NetProfit.yoy` |
| `operating_cash_to_revenue` | `NetOperateCashFlow / OperatingRevenue * 100` |

### 利润表 `income`

| TickFlow 字段 | Longbridge 字段 |
|---|---|
| `revenue` | `OperatingRevenue.value` |
| `operating_profit` | `OperatingIncome.value` |
| `net_income` | `NetProfit.value` |
| `basic_eps` | `EPS.value` |

### 资产负债表 `balance_sheet`

| TickFlow 字段 | Longbridge 字段或计算方式 |
|---|---|
| `total_assets` | `TotalAssets.value` |
| `cash_and_equivalents` | `CashSTInvest.value`，含短期投资 |
| `accounts_receivable` | `TotalReceiv.value` |
| `inventory` | `Inventory.value` |
| `fixed_assets` | `NPPE.value` |
| `total_liabilities` | `TotalLiability.value` |
| `total_equity` | `TotalAssets - TotalLiability` |

### 现金流量表 `cash_flow`

| TickFlow 字段 | Longbridge 字段或计算方式 |
|---|---|
| `net_operating_cash_flow` | `NetOperateCashFlow.value` |
| `net_investing_cash_flow` | `NetInvestCashFlow.value` |
| `net_financing_cash_flow` | `NetFinanceCashFlow.value` |
| `capex` | `CapEx.value` |
| `net_cash_change` | 三类现金流净额之和 |

所有记录同时保留 `symbol`、`period_end`、`report_period`、`currency`。源数据没有可靠披露日期时，`announce_date` 保持为空，不用入库时间冒充披露日期。

## 数据流

1. 财务同步服务取得 TickFlow 股票池中的 A 股、港股和美股代码。
2. `longbridge_financial` 按批查询 `lb_financial_report` 最新报告期字段。
3. 转换器一次生成四类 Polars DataFrame。
4. 现有同步服务分别覆盖写入 `data/financials/<table>/part.parquet`。
5. 现有 `/api/financials/*` 接口读取 Parquet，前端无需更换查询协议。

## 错误处理与安全

- 缺少 DSN、依赖或数据库不可达时，插件标记为不可用，不影响 TickFlow 其他功能启动。
- SQL 参数全部参数化，不拼接股票代码。
- 数据库使用独立登录角色，仅授予数据库连接、`public` schema 使用和 `lb_financial_report` 单表查询权限。
- 后台同步失败时保留上一版 Parquet，不先删除旧文件。
- 日志只记录批次、行数和异常类型，不输出密码。

## 测试与验收

- 单元测试覆盖四类字段映射、比例计算、最新报告期过滤、空值和零分母。
- 单元测试覆盖参数化批量查询与一次同步缓存。
- 能力测试覆盖选择该插件后自动授予 `financial`。
- 财务同步测试覆盖写入 Parquet 及 API 状态可用。
- 后端测试、静态检查和前端构建通过。
- 10.28 验收至少检查 `700.HK`、`9988.HK`、`AAPL.US`、`NBIS.US` 和 `600519.SH`，四个接口返回实际数据，财务页不再显示 Expert 锁定。

