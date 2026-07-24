# TickFlow 三市场 ClickHouse 与共享策略接入实施计划

> **供执行代理使用：** 必须使用 `superpowers:executing-plans` 按任务逐项实施；每个生产行为先执行 `superpowers:test-driven-development` 的 RED-GREEN-REFACTOR。步骤使用复选框跟踪。

**目标：** 在保留 TickFlow UI 的前提下，接入 10.28 ClickHouse 的 A/H/US 历史与实时行情，将已实现的三市场结构突破策略接入 TickFlow 矩阵回测，并引入三市场交易规则。

**架构：** Longbridge 继续作为唯一实时行情写入端；TickFlow 通过内置 ClickHouse Provider 读取数据并物化到现有 Parquet 热路径。结构突破扫描器以 `codex/structure-breakout-scanner` 分支的版本化 wheel 作为共享策略核心，TickFlow 只实现适配器。市场规则通过独立、纯函数化模块注入矩阵撮合，避免继续硬编码 A股 100 股一手和涨跌停语义。

**技术栈：** Python 3.11、FastAPI、Polars、NumPy、ClickHouse HTTP API、pytest、React 18、TypeScript、Vite、Docker Compose。

## 全局约束

- 所有文档、页面标签、错误信息和交付说明使用中文。
- 不建立第二套 Longbridge 实时订阅；TickFlow 只读行情表。
- 不修改现有 ClickHouse 表结构；新增表使用独立 DDL。
- 不把数据库凭证写入代码、Git 或前端响应。
- 回测信号只能在 `known_at` 之后成交。
- A股、港股、美股在第一阶段同时可用。
- 共享策略源代码只有一份；TickFlow 固定安装构建产物，不复制扫描器源文件。
- 每个任务先写失败测试并确认失败原因，再写最小实现。
- 每个任务结束运行聚焦测试和相关回归测试，`git diff --check` 通过后独立提交。

---

## 文件结构

### TickFlow 仓库

- `backend/app/market_rules.py`：市场识别、币种、手数、同日卖出和涨跌停适用规则。
- `backend/app/plugins/clickhouse/bridge.py`：ClickHouse HTTP 查询客户端和可用性检测。
- `backend/app/plugins/clickhouse/provider.py`：TickFlow Provider 契约实现。
- `backend/app/plugins/clickhouse/plugin.yaml`：内置数据源注册清单。
- `backend/app/strategy/shared_structure_breakout.py`：共享策略候选到 `SignalMatrix` 的转换。
- `backend/app/strategy/builtin/multi_market_structure_breakout.py`：TickFlow 策略注册入口和参数元数据。
- `backend/vendor/`：固定版本 wheel 与来源说明。
- `backend/app/backtest/engine.py`：使用市场规则确定手数、同日卖出和涨跌停语义。
- `backend/app/backtest/strategy.py`：结果中补充市场和币种。
- `frontend/src/pages/Backtest.tsx`：三市场标签、币种和策略证据展示。
- `frontend/src/lib/api.ts`：三市场回测响应类型。

### Longbridge 集成工作树

- `infra/realtime/clickhouse/init/003_tickflow_integration_schema.sql`：分钟K、策略运行和策略信号新增表。
- `src/longbridge_stock/tickflow_integration.py`：分钟K增量物化与策略信号写入。
- `tests/test_tickflow_integration.py`：DDL、分钟聚合和信号规范化测试。

---

### 任务 1：固定并安装共享策略核心

**文件：**

- 创建：`backend/vendor/README.md`
- 创建：`backend/vendor/longbridge_stock-0.1.0-py3-none-any.whl`
- 修改：`Dockerfile`
- 测试：`backend/tests/test_shared_strategy_dependency.py`

**接口：**

- 使用：`longbridge_stock.structure_breakout_scanner.scan_history(...)`
- 使用：`longbridge_stock.structure_breakout_scanner.DEFAULT_DETECTORS`
- 产出：TickFlow 开发环境和 Docker 镜像内均可导入固定版本策略核心。

- [ ] **步骤 1：写依赖导入失败测试**

```python
def test_structure_breakout_core_is_importable() -> None:
    from longbridge_stock.structure_breakout_scanner import scan_history

    assert callable(scan_history)
```

- [ ] **步骤 2：确认 RED**

运行：`.venv/Scripts/python.exe -m pytest tests/test_shared_strategy_dependency.py -q`

预期：因 TickFlow 虚拟环境尚未安装 `longbridge_stock` 而导入失败。

- [ ] **步骤 3：从已验证分支构建 wheel**

运行：

```powershell
python -m pip wheel --no-deps --wheel-dir E:\my_project\tickflow-stock-panel\backend\vendor E:\my_project\longbridge-stock\.worktrees\structure-breakout-scanner
```

`backend/vendor/README.md` 记录来源分支、提交 `24b24b8`、聚焦测试 `20 passed` 和重建命令。

- [ ] **步骤 4：安装并确认 GREEN**

运行：

```powershell
.venv\Scripts\python.exe -m pip install --no-deps --force-reinstall vendor\longbridge_stock-0.1.0-py3-none-any.whl
.venv\Scripts\python.exe -m pytest tests/test_shared_strategy_dependency.py -q
```

预期：1 项测试通过。

- [ ] **步骤 5：让 Docker 安装固定 wheel**

在 Python 依赖安装完成后增加：

```dockerfile
COPY backend/vendor/longbridge_stock-0.1.0-py3-none-any.whl /tmp/vendor/
RUN pip install --no-cache-dir --no-deps /tmp/vendor/longbridge_stock-0.1.0-py3-none-any.whl
```

- [ ] **步骤 6：运行构建检查并提交**

运行：`docker build --target runtime -t tickflow-shared-core-test .`

提交：`git commit -m "build: pin shared strategy core"`

### 任务 2：实现三市场规则模块

**文件：**

- 创建：`backend/app/market_rules.py`
- 创建：`backend/tests/test_market_rules.py`

**接口：**

- 产出：`MarketRule`、`market_for_symbol(symbol)`、`market_rule_for_symbol(symbol)`、`round_lot_size(symbol, metadata=None)`。

- [ ] **步骤 1：写市场识别和规则测试**

```python
def test_market_rules_cover_cn_hk_us() -> None:
    assert market_for_symbol("000001.SZ") == "cn"
    assert market_for_symbol("1.HK") == "hk"
    assert market_for_symbol("A.US") == "us"
    assert market_rule_for_symbol("000001.SZ").currency == "CNY"
    assert market_rule_for_symbol("1.HK").same_day_sell_allowed is True
    assert market_rule_for_symbol("A.US").price_limit_policy == "none"


def test_round_lot_uses_market_default_and_hk_metadata() -> None:
    assert round_lot_size("000001.SZ") == 100
    assert round_lot_size("A.US") == 1
    assert round_lot_size("1.HK", {"lot_size": 500}) == 500
```

- [ ] **步骤 2：确认 RED**

运行：`.venv/Scripts/python.exe -m pytest tests/test_market_rules.py -q`

预期：`app.market_rules` 不存在。

- [ ] **步骤 3：实现不可变规则**

```python
@dataclass(frozen=True)
class MarketRule:
    market: Literal["cn", "hk", "us"]
    timezone: str
    currency: str
    default_round_lot: int
    same_day_sell_allowed: bool
    price_limit_policy: Literal["cn", "none"]
```

未知后缀抛出 `ValueError`，不得静默按 A股处理。

- [ ] **步骤 4：确认 GREEN 并提交**

运行：`.venv/Scripts/python.exe -m pytest tests/test_market_rules.py -q`

提交：`git commit -m "feat: add three-market trading rules"`

### 任务 3：实现 ClickHouse 内置数据源插件

**文件：**

- 创建：`backend/app/plugins/clickhouse/__init__.py`
- 创建：`backend/app/plugins/clickhouse/bridge.py`
- 创建：`backend/app/plugins/clickhouse/provider.py`
- 创建：`backend/app/plugins/clickhouse/plugin.yaml`
- 创建：`backend/tests/test_clickhouse_provider.py`
- 修改：`.env.example`

**接口：**

- 使用环境变量：`CLICKHOUSE_URL`、`CLICKHOUSE_DATABASE`、`CLICKHOUSE_USER`、`CLICKHOUSE_PASSWORD`、`CLICKHOUSE_READ_TIMEOUT_SECONDS`。
- 产出：`ClickHouseProvider.get_instruments/get_daily/get_minute/get_realtime`。

- [ ] **步骤 1：写查询与字段映射测试**

使用注入的 `query_fn(sql) -> list[dict]`，断言：

```python
def test_daily_maps_turnover_to_amount_and_filters_adjusted() -> None:
    provider = ClickHouseProvider(query_fn=lambda sql: [{
        "symbol": "1.HK", "trade_date": "2026-07-17", "open": 10,
        "high": 11, "low": 9, "close": 10.5, "volume": 1000,
        "turnover": 10500, "market": "hk",
    }])
    frame = provider.get_daily(["1.HK"], None, None)
    assert frame["amount"].to_list() == [10500.0]
    assert "adjusted = 1" in provider.last_sql.lower()
```

实时测试断言 `last_done -> last_price`、`turnover -> amount`、每个 symbol 只取最新记录；分钟测试断言输出 `symbol/datetime/OHLC/volume/amount`。

- [ ] **步骤 2：确认 RED**

运行：`.venv/Scripts/python.exe -m pytest tests/test_clickhouse_provider.py -q`

预期：插件模块不存在。

- [ ] **步骤 3：实现 HTTP bridge**

`bridge.query_json_each_row(sql)` 使用 POST 请求和 `FORMAT JSONEachRow`，通过 `X-ClickHouse-User`、`X-ClickHouse-Key` 传凭证；异常信息不得包含密码。

- [ ] **步骤 4：实现 Provider 和插件清单**

`plugin.yaml` 声明 `datasets: [daily, minute, realtime]`，`check` 指向 `bridge.availability`。SQL 标识符只允许 `[A-Za-z_][A-Za-z0-9_]*`，字符串通过单引号转义。

- [ ] **步骤 5：确认 GREEN 和插件回归**

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_clickhouse_provider.py tests/test_stocksdk_provider.py -q
```

提交：`git commit -m "feat: add clickhouse market data provider"`

### 任务 4：接入三市场结构突破策略

**文件：**

- 创建：`backend/app/strategy/shared_structure_breakout.py`
- 创建：`backend/app/strategy/builtin/multi_market_structure_breakout.py`
- 创建：`backend/tests/backtest/test_shared_structure_breakout.py`

**接口：**

- 产出：`SharedStructureBreakoutMatrixStrategy.compute_signals(market, params) -> SignalMatrix`。
- 使用：共享核心的 `scan_history`、`DEFAULT_DETECTORS` 和 `StructureCandidate`。

- [ ] **步骤 1：写候选映射失败测试**

```python
def test_candidate_phases_map_to_entry_and_exit() -> None:
    signals = candidates_to_signal_matrix(
        shape=(4, 1),
        candidates=(confirmed(index=1, score=88), failed(index=3)),
    )
    assert signals.entry[:, 0].tolist() == [0, 1, 0, 0]
    assert signals.exit[:, 0].tolist() == [0, 0, 0, 1]
    assert signals.score[1, 0] == 88
```

再写测试证明 `known_at_index` 而非 `breakout_index` 决定信号位置，`RETEST_CONFIRMED` 不产生第二次加仓信号。

- [ ] **步骤 2：确认 RED**

运行：`.venv/Scripts/python.exe -m pytest tests/backtest/test_shared_structure_breakout.py -q`

预期：适配器模块不存在。

- [ ] **步骤 3：实现矩阵到标准行转换**

忽略 `close` 非有限值的单元格；`market` 根据 symbol 后缀解析；`turnover` 优先使用 `amount` 字段，否则使用 `close * volume`。

- [ ] **步骤 4：实现候选到 SignalMatrix 转换**

`BREAKOUT_CONFIRMED` 和无更早入场的 `RETEST_CONFIRMED` 产生 ENTRY；`FAILED/INVALIDATED` 产生 EXIT；`ACCELERATION` 只更新 score。所有索引使用 `known_at_index`。

- [ ] **步骤 5：注册内置策略**

策略元数据包含三市场、日线周期、参数 schema、`EXECUTION_BACKEND="matrix_native"`、`MATRIX_STRATEGY`、默认止损和最大持有天数。

- [ ] **步骤 6：确认 GREEN 和策略注册回归**

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/backtest/test_shared_structure_breakout.py tests/test_strategy_registry.py -q
```

提交：`git commit -m "feat: adapt shared structure breakout strategy"`

### 任务 5：让矩阵撮合遵守三市场规则

**文件：**

- 修改：`backend/app/backtest/engine.py`
- 修改：`backend/app/backtest/strategy.py`
- 创建：`backend/tests/backtest/test_multi_market_execution.py`

**接口：**

- 使用：`market_rule_for_symbol`、`round_lot_size`。
- 产出：交易记录中的 `market`、`currency`、正确手数和同日卖出行为。

- [ ] **步骤 1：写手数失败测试**

用三只价格相同的测试证券分别运行矩阵组合撮合，断言 A股股数为 100 的倍数、美股为整数股、港股按测试元数据的 500 股手数取整。

- [ ] **步骤 2：写同日卖出和涨跌停失败测试**

构造同一交易日本地分钟时间标签：A股持仓不得当日退出，港股和美股可以；HK/US 即使 `limit_up_locked=True` 也不得套用 A股一字板阻塞。

- [ ] **步骤 3：确认 RED**

运行：`.venv/Scripts/python.exe -m pytest tests/backtest/test_multi_market_execution.py -q`

预期：现有撮合固定 100 股且统一跳过当日退出，测试失败。

- [ ] **步骤 4：注入市场规则**

四条撮合路径统一调用私有辅助函数 `_round_lot_for_symbol`、`_same_day_sell_allowed` 和 `_price_limit_applies`，不得复制判断。

- [ ] **步骤 5：扩展交易记录**

`TradeRecord` 增加 `market`、`currency`，序列化结果同步输出。

- [ ] **步骤 6：确认 GREEN 和回测回归**

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/backtest/test_multi_market_execution.py tests/backtest/test_engine_portfolio.py tests/backtest/test_strategy_backtest_correctness.py -q
```

提交：`git commit -m "feat: apply market rules in backtests"`

### 任务 6：新增 ClickHouse 表与分钟K物化

**文件：**

- 创建：`infra/realtime/clickhouse/init/003_tickflow_integration_schema.sql`
- 创建：`src/longbridge_stock/tickflow_integration.py`
- 创建：`tests/test_tickflow_integration.py`

**接口：**

- 产出：`ensure_tickflow_tables()`、`materialize_minute_bars(start, end)`、`insert_strategy_run(row)`、`insert_strategy_signals(rows)`。

- [ ] **步骤 1：在新 Longbridge 工作树写 DDL 失败测试**

测试读取 SQL 文件并断言三个表、UTC 时间、分区键和排序键存在；信号规范化测试断言 `known_at_utc` 缺失时拒绝写入。

- [ ] **步骤 2：确认 RED**

运行：`python -m pytest tests/test_tickflow_integration.py -q`

- [ ] **步骤 3：实现增量 DDL**

创建 `lb_minute_bars`、`lb_strategy_runs`、`lb_strategy_signals`，使用 `ReplacingMergeTree(updated_at)`；不执行任何现有表 ALTER。

- [ ] **步骤 4：实现分钟聚合**

对 `lb_intraday_lines` 按市场、symbol、分钟聚合：`argMin(price,line_time)`、`max(price)`、`min(price)`、`argMax(price,line_time)`；成交量和成交额使用相邻累计值差并对负差归零。`known_at_utc` 为该分钟结束时间。

- [ ] **步骤 5：实现信号写入校验**

拒绝未知 action、空策略版本、空 `known_at_utc` 和 `known_at_utc < signal_time_utc` 的记录；JSON 字段稳定序列化。

- [ ] **步骤 6：确认 GREEN 并提交 Longbridge 分支**

运行：

```powershell
python -m pytest tests/test_tickflow_integration.py tests/test_structure_breakout_scanner.py tests/test_scan_structure_breakouts.py -q
```

提交：`git commit -m "feat: add tickflow integration storage"`

### 任务 7：扩展回测 UI 的市场和币种展示

**文件：**

- 修改：`frontend/package.json`
- 修改：`frontend/pnpm-lock.yaml`
- 修改：`frontend/src/lib/api.ts`
- 修改：`frontend/src/pages/Backtest.tsx`
- 创建：`frontend/src/lib/market-display.ts`
- 创建：`frontend/src/lib/market-display.test.ts`

**接口：**

- 使用：交易记录 `market`、`currency`。
- 产出：三市场标签、币种列和按市场汇总卡片。

- [ ] **步骤 1：安装测试运行器并写纯函数测试**

运行：`pnpm add -D vitest`，并在 `package.json` 增加 `"test": "vitest"`。将市场标签与币种展示提取为纯函数 `marketLabel`、`currencyLabel`，先创建测试：

```typescript
import { describe, expect, it } from 'vitest'
import { currencyLabel, marketLabel } from './market-display'

describe('market display', () => {
  it('maps three markets to Chinese labels', () => {
    expect(marketLabel('cn')).toBe('A股')
    expect(marketLabel('hk')).toBe('港股')
    expect(marketLabel('us')).toBe('美股')
  })

  it('keeps unknown values visible', () => {
    expect(marketLabel('other')).toBe('other')
    expect(currencyLabel('USD')).toBe('美元')
  })
})
```

- [ ] **步骤 2：确认 RED**

运行：`pnpm test --run src/lib/market-display.test.ts`

预期：因 `market-display.ts` 不存在而失败。

- [ ] **步骤 3：扩展 API 类型和页面**

交易表增加市场和币种列；结果顶部按 `market` 分组三张摘要卡。保留现有布局、颜色、间距和图表。

- [ ] **步骤 4：确认 GREEN 和生产构建**

运行：

```powershell
pnpm test --run src/lib/market-display.test.ts
pnpm run build
```

提交：`git commit -m "feat: show markets and currencies in backtests"`

### 任务 8：集成验证与 10.28 部署

**文件：**

- 修改：`.env.example`
- 修改：`docker-compose.yml`
- 修改：`docs/deployment.md`

**接口：**

- 产出：10.28 上可选择 ClickHouse Provider、导入三市场数据、运行结构突破回测。

- [x] **步骤 1：运行完整本地验证**

```powershell
cd backend
.venv\Scripts\python.exe -m pytest -q
cd ..\frontend
npm run build
cd ..
git diff --check
```

预期：后端零失败，前端构建退出码 0，无空白错误。

- [x] **步骤 2：在 10.28 执行新增 DDL**

只执行 `003_tickflow_integration_schema.sql`，随后通过 `system.tables` 和 `system.columns` 核对三个新表；不得删除或 ALTER 现有表。

- [x] **步骤 3：构建并替换 TickFlow 容器**

保留当前镜像标签作为回滚点；新镜像固定安装共享策略 wheel，并配置 ClickHouse 只读凭证和三个功能开关。

- [x] **步骤 4：运行线上烟雾测试**

验证：

- `/api/health` 返回成功。
- ClickHouse Provider 能返回 CN/HK/US 各一个日K样例。
- 实时接口返回最新三市场快照并包含数据时间。
- 策略注册接口包含 `multi_market_structure_breakout`。
- 三市场小样本回测可生成交易记录、市场和币种字段。
- 原有 TickFlow 首页、选股、回测和设置页面可访问。

- [x] **步骤 5：最终提交和状态核对**

运行：`git status --short`、`git log --oneline -10`，确认只存在计划内提交和无意外未跟踪文件。

## 最终自审清单

- [x] 规格中的数据契约、三市场规则、共享策略、UI、异常和回滚均有对应任务。
- [x] 计划中没有占位描述或未定义接口。
- [x] `known_at`、市场代码、币种和信号阶段命名在所有任务中一致。
- [x] 共享策略核心未复制到 TickFlow 源码。
- [x] 线上部署不建立第二套 Longbridge 订阅。
