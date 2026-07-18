# Longbridge 财务数据接入实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 PostgreSQL `lb_financial_report` 的 A 股、港股和美股财务数据转换并接入 TickFlow 既有财务页、API、AI 分析和自动同步链路。

**Architecture:** 新增内置 `longbridge_financial` 插件，以只读 DSN 和参数化 SQL 查询最新报告期字段，并在内存中转换成四类标准 Polars DataFrame。现有财务同步服务继续负责 Parquet 落盘和 API 读取，同时扩展自定义财务能力授予及可配置的每日自动同步。

**Tech Stack:** Python 3.11、FastAPI、Polars、psycopg 3、pytest、React/Vite、Docker Compose、PostgreSQL 18。

## Global Constraints

- 不修改 TickFlow 财务页的四类 API 协议。
- 不复制 PostgreSQL 的 11 GB 原始财务表到 ClickHouse。
- 数据库账号仅拥有 `lb_financial_report` 的 `SELECT` 权限。
- 不用 `updated_at` 冒充公告日期。
- 自动同步默认关闭，仅由 `FINANCIAL_AUTO_SYNC_ENABLED=true` 开启。
- 所有生产代码必须先有能正确失败的测试。

---

### Task 1: PostgreSQL 财务字段转换器

**Files:**
- Create: `backend/app/plugins/longbridge_financial/__init__.py`
- Create: `backend/app/plugins/longbridge_financial/provider.py`
- Test: `backend/tests/test_longbridge_financial_provider.py`

**Interfaces:**
- Consumes: `query_fn(sql: str, params: tuple) -> list[dict]`。
- Produces: `LongbridgeFinancialProvider.get_financials(table: str, symbols: list[str], latest_only: bool = True) -> pl.DataFrame`。

- [ ] **Step 1: 写四类字段映射失败测试**

用 `700.HK` 同一报告期的 `EPS`、`BPS`、`ROE`、`OperatingRevenue`、`NetProfit`、`TotalAssets`、`TotalLiability`、`NetOperateCashFlow`、`NetInvestCashFlow`、`NetFinanceCashFlow` 和 `CapEx` 构造字段级记录，分别断言四类输出包含设计文档中的字段和派生比例。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python -m pytest backend/tests/test_longbridge_financial_provider.py -q`

Expected: FAIL，原因是 `app.plugins.longbridge_financial.provider` 尚不存在。

- [ ] **Step 3: 实现最小转换器**

实现以下核心结构：

```python
class LongbridgeFinancialProvider:
    name = "longbridge_financial"
    builtin = True
    capabilities = ProviderCapabilities(financial=True)

    def get_financials(
        self, table: str, symbols: list[str], latest_only: bool = True
    ) -> pl.DataFrame:
        frames = self._load_frames(symbols)
        return frames[table].clone()
```

转换时先按股票选择最大 `fp_end`，只保留该报告期记录，再按字段名建立字典。百分比派生函数遇到空值或零分母返回 `None`。

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `python -m pytest backend/tests/test_longbridge_financial_provider.py -q`

Expected: PASS。

- [ ] **Step 5: 增加最新报告期、空值和缓存测试**

增加以下行为：

- 较旧报告期不能覆盖最新报告期。
- `TotalAssets=0` 时负债率为 `None`。
- 连续请求四张表只调用一次原始查询。
- 未知表名抛出明确 `ValueError`。

- [ ] **Step 6: 再次运行测试并提交**

Run: `python -m pytest backend/tests/test_longbridge_financial_provider.py -q`

Expected: PASS。

Commit: `feat: add longbridge financial provider`

---

### Task 2: PostgreSQL 只读桥接和插件注册

**Files:**
- Create: `backend/app/plugins/longbridge_financial/bridge.py`
- Create: `backend/app/plugins/longbridge_financial/plugin.yaml`
- Modify: `backend/pyproject.toml`
- Modify: `docker-compose.yml`
- Test: `backend/tests/test_longbridge_financial_provider.py`

**Interfaces:**
- Consumes: `LONGBRIDGE_FINANCIAL_DSN`、`LONGBRIDGE_FINANCIAL_BATCH_SIZE`、`LONGBRIDGE_FINANCIAL_TIMEOUT_SECONDS`。
- Produces: `bridge.query_rows(sql, params)` 和 `bridge.availability() -> tuple[bool, str]`。

- [ ] **Step 1: 写参数化查询失败测试**

断言查询 SQL 使用 `symbol = ANY(%s)`、`field = ANY(%s)` 和 `DISTINCT ON (symbol, field)`，股票代码仅出现在参数中；超过批大小时拆成多次查询。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python -m pytest backend/tests/test_longbridge_financial_provider.py -q`

Expected: FAIL，原因是尚未生成查询和批处理。

- [ ] **Step 3: 实现只读桥接与批量查询**

`bridge.py` 延迟导入 `psycopg`，使用 `dict_row`，设置只读事务并返回字典列表。插件清单声明：

```yaml
name: longbridge_financial
display_name: "Longbridge PostgreSQL 财务数据"
runtime: none
entry: app.plugins.longbridge_financial.provider:LongbridgeFinancialProvider
check: app.plugins.longbridge_financial.bridge:availability
datasets: [financial]
```

在后端依赖中加入 `psycopg[binary]>=3.1`，Docker Compose 传入上述三个环境变量。

- [ ] **Step 4: 运行测试确认 GREEN 并提交**

Run: `python -m pytest backend/tests/test_longbridge_financial_provider.py -q`

Expected: PASS。

Commit: `feat: register postgres financial plugin`

---

### Task 3: 自定义财务能力与同步链路

**Files:**
- Modify: `backend/app/tickflow/policy.py`
- Modify: `backend/app/services/financial_sync.py`
- Test: `backend/tests/test_financial_custom_source.py`

**Interfaces:**
- Consumes: `preferences.get_financial_provider()` 和 `custom_sources.provider_has_dataset(provider, "financial")`。
- Produces: 自定义财务源可用时 `CapabilitySet.has(Cap.FINANCIAL) == True`；`sync_all` 和调度器均允许自定义财务源。

- [ ] **Step 1: 写能力和同步失败测试**

测试选择 `longbridge_financial` 时 `_augment_custom_sources` 授予 `Cap.FINANCIAL`；无 TickFlow Expert 能力时，自定义源仍可初始化调度器和执行 `sync_all`。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python -m pytest backend/tests/test_financial_custom_source.py -q`

Expected: FAIL，当前能力补充只处理分钟数据，`sync_all` 和调度器启动仍只判断 TickFlow 能力。

- [ ] **Step 3: 实现最小能力补充和同步放行**

在 `_augment_custom_sources` 中增加财务源判断并调用：

```python
capset.grant(Cap.FINANCIAL)
```

把 `sync_all`、`FinancialScheduler.start` 的门控统一为“TickFlow 财务能力或当前自定义财务源可用”。

- [ ] **Step 4: 运行测试确认 GREEN 并提交**

Run: `python -m pytest backend/tests/test_financial_custom_source.py -q`

Expected: PASS。

Commit: `fix: grant custom financial capability`

---

### Task 4: 可配置自动财务同步

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/services/financial_sync.py`
- Test: `backend/tests/test_financial_custom_source.py`

**Interfaces:**
- Consumes: `FINANCIAL_AUTO_SYNC_ENABLED`、`FINANCIAL_SYNC_STARTUP_DELAY_SECONDS`、`FINANCIAL_SYNC_INTERVAL_SECONDS`。
- Produces: 自动模式启动后延迟同步全部四表，之后按间隔重复；默认关闭。

- [ ] **Step 1: 写自动同步失败测试**

使用极短启动延迟和间隔，替换 `_run_body` 为记录调用的函数，断言自动模式调用全部同步、停止后不再调用，并且手动与自动同步共享互斥状态。

- [ ] **Step 2: 运行测试确认 RED**

Run: `python -m pytest backend/tests/test_financial_custom_source.py -q`

Expected: FAIL，当前自动循环只同步 metrics 且时间不可配置。

- [ ] **Step 3: 实现自动同步**

Settings 增加：

```python
financial_auto_sync_enabled: bool = False
financial_sync_startup_delay_seconds: int = 60
financial_sync_interval_seconds: int = 86400
```

自动循环通过 `asyncio.to_thread(self.run_now)` 执行全部四表，避免阻塞事件循环；`main.py` 显式传入配置。

- [ ] **Step 4: 运行测试确认 GREEN 并提交**

Run: `python -m pytest backend/tests/test_financial_custom_source.py -q`

Expected: PASS。

Commit: `feat: schedule financial source refresh`

---

### Task 5: 本地回归验证

**Files:**
- Modify only if a failing test exposes an in-scope defect.

- [ ] **Step 1: 运行新增测试**

Run: `python -m pytest backend/tests/test_longbridge_financial_provider.py backend/tests/test_financial_custom_source.py -q`

Expected: 全部 PASS。

- [ ] **Step 2: 运行后端完整测试和静态检查**

Run: `python -m pytest backend/tests -q`

Run: `python -m ruff check backend/app backend/tests`

Expected: 0 failures，0 lint errors。

- [ ] **Step 3: 构建前端**

Run: `npm --prefix frontend run build`

Expected: exit 0。

- [ ] **Step 4: 检查差异并提交验证修复**

Run: `git diff --check && git status --short`

Expected: 无空白错误，仅包含本任务文件。

---

### Task 6: 10.28 安全部署与真实数据验收

**Files:**
- Modify on server: `/home/alwin/apps/tickflow-stock-panel/.env`
- Modify in PostgreSQL: create/login role `tickflow_financial_reader` and grant read-only permissions.

- [ ] **Step 1: 创建最小权限数据库账号**

生成随机密码，创建或更新 `tickflow_financial_reader`，只授予：

```sql
GRANT CONNECT ON DATABASE longbridge_stock TO tickflow_financial_reader;
GRANT USAGE ON SCHEMA public TO tickflow_financial_reader;
GRANT SELECT ON TABLE public.lb_financial_report TO tickflow_financial_reader;
```

- [ ] **Step 2: 配置环境并构建镜像**

在 `.env` 写入 DSN 和：

```dotenv
FINANCIAL_AUTO_SYNC_ENABLED=true
FINANCIAL_SYNC_STARTUP_DELAY_SECONDS=60
FINANCIAL_SYNC_INTERVAL_SECONDS=86400
```

构建带唯一时间标签的镜像，保留上一镜像以便回滚。

- [ ] **Step 3: 切换数据源并执行首次同步**

调用受认证设置接口把 `financial_data_provider` 改为 `longbridge_financial`，重启后调用 `/api/financials/sync/all`，轮询 `/api/financials/status` 直到 `syncing=false`。

- [ ] **Step 4: 验证真实接口**

分别请求：

- `/api/financials/metrics?symbol=700.HK`
- `/api/financials/income?symbol=9988.HK`
- `/api/financials/balance-sheet?symbol=AAPL.US`
- `/api/financials/cash-flow?symbol=NBIS.US`
- `/api/financials/metrics?symbol=600519.SH`

Expected: HTTP 200，返回非空数据，`period_end` 和关键数值与 PostgreSQL 源记录一致。

- [ ] **Step 5: 浏览器验收**

打开 `http://alwinzhang.com:3018/financials?market=hk`，确认不再显示 Expert 锁定；搜索腾讯和阿里后四个页签显示数据。切换美股确认 AAPL 和 NBIS。

- [ ] **Step 6: 检查日志和资源**

确认插件注册成功、同步没有密码泄露、容器健康、自动同步任务已启动，PostgreSQL 没有持续长查询或异常连接积压。

