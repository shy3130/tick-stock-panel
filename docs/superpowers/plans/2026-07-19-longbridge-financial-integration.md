# 财务数据 ClickHouse 统一接入实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 PostgreSQL `lb_financial_report` 瘦身迁移到 ClickHouse，并让 TickFlow 财务页仅通过 ClickHouse 查询 A 股、港股和美股财务数据。

**Architecture:** 独立迁移脚本通过只读 PostgreSQL 账号流式读取结构化列，去掉 `id` 和 `payload` 后写入 ClickHouse `ReplacingMergeTree`。现有 ClickHouse Provider 增加 `financial` 数据集，把最新报告期字段转换为 TickFlow 四类财务记录，现有 Parquet、API 和 UI 保持不变。

**Tech Stack:** Python 3.11、Polars、psycopg 3、ClickHouse HTTP API、pytest、Docker Compose、PostgreSQL 18。

## Global Constraints

- TickFlow 在线请求不直接连接 PostgreSQL。
- 不迁移 PostgreSQL 的 `id` 和 `payload`。
- PostgreSQL 账号只拥有 `lb_financial_report` 的 `SELECT` 权限。
- 迁移和增量同步必须可重复执行、可断点续跑。
- 不用 `updated_at` 冒充公告日期。
- 所有生产行为先写失败测试。

---

### Task 1: 撤销未部署的 PostgreSQL 在线插件

**Files:**
- Delete: `backend/app/plugins/longbridge_financial/__init__.py`
- Delete: `backend/app/plugins/longbridge_financial/bridge.py`
- Delete: `backend/app/plugins/longbridge_financial/provider.py`
- Delete: `backend/app/plugins/longbridge_financial/plugin.yaml`
- Delete: `backend/tests/test_longbridge_financial_provider.py`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: 当前工作区未提交的 PG 直连实现。
- Produces: TickFlow 在线服务不包含 PostgreSQL 财务插件或 DSN 环境变量。

- [ ] 使用 `apply_patch` 删除上述未部署文件和 Compose 中 PG 在线连接变量。
- [ ] 保留后续迁移脚本需要的 `psycopg[binary]>=3.1` 依赖。
- [ ] 运行 `git diff --check`，确认没有误删其他数据源改造。

---

### Task 2: ClickHouse 财务表和流式迁移器

**Files:**
- Create: `backend/scripts/sync_financials_pg_to_clickhouse.py`
- Create: `backend/tests/test_financial_clickhouse_migration.py`

**Interfaces:**
- Consumes: PostgreSQL 字典行、`POSTGRES_FINANCIAL_DSN`、`CLICKHOUSE_URL`、`CLICKHOUSE_DATABASE`。
- Produces: `normalize_source_row(row) -> dict`、`FinancialMigration.run() -> MigrationSummary`。

- [ ] **Step 1: 写源行清洗失败测试**

测试 `Decimal`、空字符串、日期、时区时间、布尔值和 `1/32` 行业排名转换为 ClickHouse JSONEachRow；断言输出没有 `id` 和 `payload`。

- [ ] **Step 2: 运行确认 RED**

Run: `backend/.venv/Scripts/python -m pytest backend/tests/test_financial_clickhouse_migration.py -q`

Expected: FAIL，迁移模块不存在。

- [ ] **Step 3: 实现表结构和最小清洗器**

脚本内定义 `CREATE TABLE IF NOT EXISTS longbridge.lb_financial_report`，使用 `ReplacingMergeTree(updated_at)`、`PARTITION BY fiscal_year` 和业务唯一键排序。

- [ ] **Step 4: 运行确认 GREEN**

Run: `backend/.venv/Scripts/python -m pytest backend/tests/test_financial_clickhouse_migration.py -q`

Expected: PASS。

- [ ] **Step 5: 写分批、checkpoint 和重叠窗口失败测试**

注入假的源游标和 ClickHouse 写入器，断言固定批次、每批成功更新 checkpoint、增量起点回退 5 分钟、失败批次不前移 checkpoint。

- [ ] **Step 6: 实现流式迁移并运行 GREEN**

PostgreSQL 使用命名服务端游标和 `fetchmany`；ClickHouse 使用 JSONEachRow HTTP 分批写入；checkpoint 原子替换保存。

- [ ] **Step 7: 提交**

Commit: `feat: migrate financial reports to clickhouse`

---

### Task 3: ClickHouse Provider 财务转换

**Files:**
- Create: `backend/app/plugins/clickhouse/financial.py`
- Modify: `backend/app/plugins/clickhouse/provider.py`
- Modify: `backend/app/plugins/clickhouse/plugin.yaml`
- Test: `backend/tests/test_clickhouse_financial_provider.py`

**Interfaces:**
- Consumes: `QueryFn(sql) -> list[dict]` 返回字段级最新记录。
- Produces: `ClickHouseProvider.get_financials(table, symbols, latest_only=True) -> pl.DataFrame`。

- [ ] **Step 1: 写四类映射失败测试**

用 `700.HK` 最新报告期字段断言 metrics、income、balance_sheet、cash_flow 的设计字段和派生比例；连续读取四表只执行一次查询。

- [ ] **Step 2: 运行确认 RED**

Run: `backend/.venv/Scripts/python -m pytest backend/tests/test_clickhouse_financial_provider.py -q`

Expected: FAIL，ClickHouse Provider 尚无 `get_financials`。

- [ ] **Step 3: 实现最小查询与转换**

查询使用固定财务字段、转义后的股票列表、`FINAL` 和 `LIMIT 1 BY symbol, field`。转换器只保留每只股票最大 `fp_end`，并输出 `symbol`、`period_end`、`report_period`、`currency` 和四类映射字段。

- [ ] **Step 4: 运行确认 GREEN**

Run: `backend/.venv/Scripts/python -m pytest backend/tests/test_clickhouse_financial_provider.py -q`

Expected: PASS。

- [ ] **Step 5: 增加空值、零分母、旧报告期和股票批次测试**

每个行为单独测试，并确认 SQL 不包含未经转义的输入。

- [ ] **Step 6: 提交**

Commit: `feat: serve financials from clickhouse`

---

### Task 4: 财务能力和现有同步链路

**Files:**
- Modify: `backend/app/tickflow/policy.py`
- Modify: `backend/app/services/financial_sync.py`
- Test: `backend/tests/test_financial_custom_source.py`

**Interfaces:**
- Consumes: `preferences.get_financial_provider() == "clickhouse"` 和插件 `financial` 数据集。
- Produces: `Cap.FINANCIAL`、手动全部同步、单表同步和 last_sync 恢复。

- [ ] **Step 1: 运行现有 RED 测试并把源名改为 clickhouse**

Run: `backend/.venv/Scripts/python -m pytest backend/tests/test_financial_custom_source.py -q`

Expected: 未完成实现时至少一项 FAIL。

- [ ] **Step 2: 实现能力补充和自定义财务门控**

`_augment_custom_sources` 为 ClickHouse 财务源授予 `Cap.FINANCIAL`；`sync_all` 和 `FinancialScheduler.start` 接受自定义财务源。

- [ ] **Step 3: 运行确认 GREEN 并提交**

Run: `backend/.venv/Scripts/python -m pytest backend/tests/test_financial_custom_source.py -q`

Expected: PASS。

Commit: `fix: enable clickhouse financial capability`

---

### Task 5: 本地验证

**Files:**
- Modify only when a failing verification exposes an in-scope defect.

- [ ] Run: `backend/.venv/Scripts/python -m pytest backend/tests/test_financial_clickhouse_migration.py backend/tests/test_clickhouse_financial_provider.py backend/tests/test_financial_custom_source.py -q`
- [ ] Run: `backend/.venv/Scripts/python -m pytest backend/tests -q`
- [ ] Run: `backend/.venv/Scripts/python -m ruff check backend/app backend/tests backend/scripts/sync_financials_pg_to_clickhouse.py`
- [ ] Run: `npm --prefix frontend run build`
- [ ] Run: `git diff --check`

Expected: 全部 exit 0。

---

### Task 6: 10.28 迁移、增量任务和 TickFlow 部署

**Files/State:**
- Create ClickHouse table: `longbridge.lb_financial_report`
- Configure server secret: `/home/alwin/apps/tickflow-stock-panel/.env`
- Create timer/service for daily incremental migration.

- [ ] 创建 `tickflow_financial_reader` PostgreSQL 只读角色。
- [ ] 先迁移一个股票并对比 PostgreSQL/ClickHouse 字段。
- [ ] 执行 497 万行全量迁移，监控批次、内存、查询耗时和 ClickHouse 磁盘占用。
- [ ] 核对总行数、股票数、最大 `updated_at`、A/H/美股抽样数据。
- [ ] 在 10.28 Chronicle 中配置每天增量任务，使用 5 分钟重叠窗口。
- [ ] 构建并部署 TickFlow 镜像，财务数据源切换为 `clickhouse`。
- [ ] 触发 `/api/financials/sync/all`，轮询完成。
- [ ] 验证 `700.HK`、`9988.HK`、`AAPL.US`、`NBIS.US`、`600519.SH` 四类接口和页面。
- [ ] 记录 ClickHouse 压缩后占用，并确认日志没有凭据和持续慢查询。
