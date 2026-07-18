# Longbridge 财务数据 ClickHouse 统一接入设计

## 背景与目标

TickFlow 财务页已经具备财务指标、利润表、资产负债表、现金流量表和 AI 分析能力，但 10.28 当前没有 TickFlow Expert Key，也没有本地财务 Parquet。

现有财务明细位于 PostgreSQL `longbridge_stock.lb_financial_report`，约 497 万行、11 GB，覆盖 A 股、港股和美股。ClickHouse 当前只有行情和 F10 公司资料，没有利润表、资产负债表、现金流量表明细。

本次目标：

1. 将结构化财务明细迁移到 ClickHouse，统一分析数据管理。
2. 控制数据体积，不迁移重复且暂未用于分析的原始 JSON `payload`。
3. 建立可重复、可断点续跑的增量同步。
4. TickFlow 财务页只读 ClickHouse，不直接连接 PostgreSQL。

## 方案选择

### 采用方案：瘦身明细表 + 增量物化 + ClickHouse Provider

在 ClickHouse 建立 `longbridge.lb_financial_report`，保留字段级财务数据，但删除 PostgreSQL 自增 `id` 和大体积 `payload`。迁移脚本通过只读 PostgreSQL 账号流式读取，分批写入 ClickHouse HTTP 接口。TickFlow 现有 `clickhouse` 插件增加 `financial` 数据集，从该表查询每只股票最新报告期并转换成四类标准记录。

采用理由：

- ClickHouse 成为 TickFlow 和分析任务的统一查询入口。
- 去掉 `payload` 后显著减少 11 GB 源表的重复 JSON 占用。
- 不复制成四张重复宽表，保留一张通用字段明细即可支持后续指标扩展。
- PostgreSQL 迁移连接只属于离线物化任务，不进入 TickFlow 在线请求链路。

### 未采用方案

1. TickFlow 在线直连 PostgreSQL：实现直接，但继续形成双查询入口，不符合统一管理目标。
2. ClickHouse `MaterializedPostgreSQL` 实时复制：会复制 11 GB `payload`，还需要 PostgreSQL 逻辑复制和 WAL 运维。
3. 分别建立四张 ClickHouse 宽表：页面查询快，但字段重复、历史扩展和三市场差异维护成本更高。

## ClickHouse 表设计

```sql
CREATE TABLE IF NOT EXISTS longbridge.lb_financial_report
(
    symbol String,
    market LowCardinality(String),
    report_type LowCardinality(String),
    report_period LowCardinality(String),
    fiscal_year UInt16,
    fp_end Nullable(Date),
    field LowCardinality(String),
    name String,
    value Nullable(Float64),
    ratio Nullable(Float64),
    yoy Nullable(Float64),
    percent Bool,
    industry_ranking String,
    ranking_code LowCardinality(String),
    currency LowCardinality(String),
    updated_at DateTime64(3, 'Asia/Shanghai')
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY fiscal_year
ORDER BY (symbol, report_type, field, fiscal_year, report_period);
```

设计说明：

- `ReplacingMergeTree(updated_at)` 允许增量任务重复覆盖同一业务键。
- `PARTITION BY fiscal_year` 便于按财年裁剪和维护。
- 排序键与 PostgreSQL 唯一键一致，支持股票和字段查询。
- `ratio`、`yoy` 从文本安全转换为可计算的 `Nullable(Float64)`。
- `industry_ranking` 保留字符串，因为源值可能是 `1/32`。
- 不迁移 `payload`；如果以后确需原始审计数据，仍可从 PostgreSQL 或原始快照归档读取。

## 迁移与增量同步

新增独立脚本 `backend/scripts/sync_financials_pg_to_clickhouse.py`：

1. 校验 ClickHouse 表，不存在则创建。
2. 读取 ClickHouse 目标表的最大 `updated_at`。
3. 首次运行流式读取 PostgreSQL 全表；增量运行从最大时间减去 5 分钟作为重叠窗口。
4. 使用服务端游标和固定批次读取，避免把 497 万行一次装入内存。
5. 转成 JSONEachRow 后分批写 ClickHouse。
6. 每批成功后更新本地 checkpoint；失败时保留已写批次，可安全重跑。
7. 完成后执行行数、股票数、最大报告期和抽样字段校验。

迁移期间不删除 PostgreSQL 数据。TickFlow 验收稳定后，PostgreSQL 表只保留为过渡源；后续财务采集可改为直接写 ClickHouse，再决定是否归档旧表。

## TickFlow 财务读取

现有 `ClickHouseProvider` 增加：

```python
def get_financials(
    self,
    table: str,
    symbols: list[str],
    latest_only: bool = True,
) -> pl.DataFrame:
    ...
```

Provider 按股票批量查询字段最新记录，一次生成四类 DataFrame，并短时缓存结果，避免“全部同步”连续四次扫描同一批数据。

### 财务指标 `metrics`

| TickFlow 字段 | ClickHouse 字段或计算方式 |
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

| TickFlow 字段 | ClickHouse 字段 |
|---|---|
| `revenue` | `OperatingRevenue.value` |
| `operating_profit` | `OperatingIncome.value` |
| `net_income` | `NetProfit.value` |
| `basic_eps` | `EPS.value` |

### 资产负债表 `balance_sheet`

| TickFlow 字段 | ClickHouse 字段或计算方式 |
|---|---|
| `total_assets` | `TotalAssets.value` |
| `cash_and_equivalents` | `CashSTInvest.value`，含短期投资 |
| `accounts_receivable` | `TotalReceiv.value` |
| `inventory` | `Inventory.value` |
| `fixed_assets` | `NPPE.value` |
| `total_liabilities` | `TotalLiability.value` |
| `total_equity` | `TotalAssets - TotalLiability` |

### 现金流量表 `cash_flow`

| TickFlow 字段 | ClickHouse 字段或计算方式 |
|---|---|
| `net_operating_cash_flow` | `NetOperateCashFlow.value` |
| `net_investing_cash_flow` | `NetInvestCashFlow.value` |
| `net_financing_cash_flow` | `NetFinanceCashFlow.value` |
| `capex` | `CapEx.value` |
| `net_cash_change` | 三类现金流净额之和 |

所有记录保留 `symbol`、`period_end`、`report_period`、`currency`。源数据没有可靠披露日期时，`announce_date` 保持为空。

## 能力与同步

- `clickhouse` 插件清单增加 `financial` 数据集。
- 选择 ClickHouse 财务源时，运行时授予 `financial` 能力，不依赖 TickFlow Expert Key。
- 复用现有手动“全部同步”和单表同步，继续输出小体积 Parquet 给现有 API。
- PostgreSQL → ClickHouse 增量任务每天由 10.28 的 Chronicle 统一调度；TickFlow ClickHouse → Parquet 同步在迁移任务完成后触发。
- 两级任务都可重复执行，失败不删除上一版可用数据。

## 安全与错误处理

- PostgreSQL 使用专用只读角色，仅授予 `lb_financial_report` 的 `SELECT`。
- 密码只放在 10.28 的受限 `.env`，不写入 Git、日志或 ClickHouse SQL 历史。
- 所有 PostgreSQL 查询参数化；ClickHouse 标识符固定，股票代码转义。
- 批次写入失败时停止并保留 checkpoint 前的成功数据。
- 在线 TickFlow 即使增量任务失败，也继续使用上一版 Parquet。

## 测试与验收

- 单元测试覆盖源行清洗、数字/日期转换、重叠窗口和分批写入。
- 单元测试覆盖四类字段映射、最新报告期过滤、空值、零分母和查询缓存。
- 能力测试覆盖 ClickHouse 财务源自动授予 `financial`。
- 本地后端测试、静态检查和前端构建通过。
- 10.28 验收核对 PostgreSQL 与 ClickHouse 行数、股票数、最大更新时间和抽样字段。
- 页面验收至少检查 `700.HK`、`9988.HK`、`AAPL.US`、`NBIS.US`、`600519.SH`。
- 记录 ClickHouse 实际压缩后磁盘占用，与 PostgreSQL 11 GB 源表对比。
