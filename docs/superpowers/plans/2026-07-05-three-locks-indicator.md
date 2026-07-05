# 三把锁指标（三锁）重建 实现计划

> **面向 AI 代理的工作者：** REQUIRED SUB-SKILL: 使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 删除 `EChartsCandlestick.tsx` 里臆造的"MA30/60/90 粘合突破"三锁实现，换成移植自 `../fquant`（对齐"指南针"App、经 v3→v4 审查、带14个单测）的正确三锁算法（趋势锁/资金锁/形态锁），资金锁所需的历史主力净流入数据通过请求时实时读取本地资金流 CSV 补齐，不做持久化/回填。

**架构：** 分四层——① `EngineDataDiskClient` 新增一次性整档读取的资金流区间查询方法；② `FQuantProvider` 新增转发方法，磁盘模式转发、HTTP 模式返回空；③ `kline.py` 本地模式分支在 `compute_enriched` 之后合并这份数据；④ 前端新建 `threeLocks.ts` 移植算法 + 改造 `EChartsCandlestick.tsx` 的可视化。仅覆盖当前生产使用的本地磁盘模式（`is_local_daily_mode()`），非本地模式的两条子路径不在本次范围内（详见 spec 风险表）。

**技术栈：** 后端 Python 3.13 / polars / pytest（`asyncio_mode=auto`）；前端 React + TypeScript + Vite。前端无测试框架（仓库惯例），移植的 14 个用例采用与 `../fquant` 相同的"纯断言脚本 + 手动调用"风格，用 `node`（v22+ 原生支持直接运行 `.ts`，本机 v26.3.1 已验证可行）直接执行，不引入 vitest/jest 等新依赖。

## Global Constraints

- 只解决本地磁盘模式（`is_local_daily_mode()` 为真）这一条 K 线渲染路径；非本地模式（`kline.py:247+` 的"落盘表命中"与"HTTP回退现算"两条子路径）明确不处理，不新增任何 `daily_pipeline.py`/parquet schema 改动。
- 资金流历史数据源列名是 `main_net`（`engine_data_disk.py:158`），API/前端统一用目标列名 `main_net_inflow`，在新增方法内部做一次重命名，不直接透传 `main_net`。
- 三锁算法逻辑（趋势/资金/形态判定公式、MA5/10/20/60 周期、资金锁3日累计、形态锁排除自身窗口）必须与 `../fquant/web/src/components/threeLocks.ts` 逐行对齐，不自创变体。
- 资金流数据缺失（文件不存在/区间内部分日期缺行/磁盘读取异常）一律降级为 `null`（数据不足），绝不让 `GET /api/kline/daily` 报错或返回 500；`null`（数据不足）与 `false`（确定不满足）在类型层面要能区分，不能把 `null` 当 `false`。
- 只对 `asset_type == "stock"` 执行资金流合并；指数/ETF/港股的 `main_net_inflow` 恒为 `null`。
- commit 需用户授权；永不 push。

---

### Task 1: `EngineDataDiskClient.get_fund_range()` — 资金流区间查询

**文件：**
- 修改：`backend/app/data_providers/fquant/engine_data_disk.py`（紧邻既有 `get_fund_daily`，约第146-169行之后）
- 测试：`backend/tests/data_providers/test_engine_data_disk.py`

**接口：**
- Consumes：既有私有方法 `self._read(dataset, symbol_or_code, asset_type) -> pl.DataFrame`（`engine_data_disk.py:50-59`，纯 `pl.read_csv`，读整份历史 CSV、无日期过滤；路径不存在时返回空 DataFrame）。
- Produces（Task 2 依赖）：`EngineDataDiskClient.get_fund_range(code: str, start_iso: str, end_iso: str, asset_type: str | None = None) -> pl.DataFrame`，返回列 `date`/`main_net_inflow`（源列 `Date`/`Main` 重命名），按 `[start_iso, end_iso]`（闭区间）过滤，按 `date` 升序排列；标的资金流文件不存在或读取异常 → 返回空 DataFrame（沿用 `_read` 已有降级）。

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/data_providers/test_engine_data_disk.py` 末尾新增（复用文件已有的 `FUND` fixture 变量和 `make_disk` helper，只需要扩充 `FUND` 覆盖多个日期）：

```python
FUND_RANGE = """Date,Code,Main,MainRatio,SuperLarge,SuperLargeRatio,Large,LargeRatio,Medium,MediumRatio,Small,SmallRatio
2026-06-29,sh600519,100,1,50,0.5,50,0.5,-20,-0.2,-30,-0.3
2026-06-30,sh600519,-50,-0.5,10,0.1,-30,-0.3,-10,-0.1,-20,-0.2
2026-07-01,sh600519,300,3,100,1,200,2,-50,-0.5,-250,-2.5
2026-07-02,sh600519,80,0.8,40,0.4,20,0.2,10,0.1,10,0.1
"""


def make_disk_fund_range(root: Path):
    (root / "fund" / "sh600").mkdir(parents=True)
    (root / "fund" / "sh600" / "sh600519.csv").write_text(FUND_RANGE)


def test_get_fund_range_filters_to_window(tmp_path, monkeypatch):
    make_disk_fund_range(tmp_path)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    df = EngineDataDiskClient().get_fund_range("600519", "2026-06-30", "2026-07-01")

    assert df.columns == ["date", "main_net_inflow"]
    rows = df.to_dicts()
    assert [str(r["date"]) for r in rows] == ["2026-06-30", "2026-07-01"]
    assert rows[0]["main_net_inflow"] == -50.0
    assert rows[1]["main_net_inflow"] == 300.0


def test_get_fund_range_missing_symbol_returns_empty(tmp_path, monkeypatch):
    make_disk_fund_range(tmp_path)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    df = EngineDataDiskClient().get_fund_range("000001.SZ", "2026-06-30", "2026-07-01")

    assert df.is_empty()


def test_get_fund_range_full_window_beyond_available_dates(tmp_path, monkeypatch):
    make_disk_fund_range(tmp_path)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    df = EngineDataDiskClient().get_fund_range("600519", "2026-01-01", "2026-12-31")

    assert df.height == 4
    assert [str(d) for d in df["date"].to_list()] == [
        "2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02",
    ]
```

- [ ] **Step 2: 运行测试验证失败**

运行：`cd backend && uv run --extra dev pytest tests/data_providers/test_engine_data_disk.py -k get_fund_range -v`
预期：3 个测试 FAIL，报 `AttributeError: 'EngineDataDiskClient' object has no attribute 'get_fund_range'`

- [ ] **Step 3: 实现 `get_fund_range`**

在 `backend/app/data_providers/fquant/engine_data_disk.py` 里紧跟 `get_fund_daily` 方法（`:146-169`）之后插入：

```python
    def get_fund_range(self, code: str, start_iso: str, end_iso: str, asset_type: str | None = None) -> pl.DataFrame:
        """区间资金流查询 —— 一次性整档读取该标的历史 CSV,按日期过滤到 [start_iso, end_iso]。

        供三锁指标的资金锁使用: 单标的一次磁盘读取即可覆盖整个查询窗口,
        不需要按天循环调用 get_fund_daily。
        """
        df = self._read("fund", code, asset_type)
        if df.is_empty():
            return pl.DataFrame()
        return (
            df.filter(
                (pl.col("Date").cast(pl.Utf8) >= start_iso)
                & (pl.col("Date").cast(pl.Utf8) <= end_iso)
            )
            .select(
                pl.col("Date").cast(pl.Utf8).alias("date"),
                pl.col("Main").cast(pl.Float64).alias("main_net_inflow"),
            )
            .sort("date")
        )
```

- [ ] **Step 4: 运行测试验证通过**

运行：`cd backend && uv run --extra dev pytest tests/data_providers/test_engine_data_disk.py -v`
预期：全部 PASS（含新增 3 个 + 原有测试）

- [ ] **Step 5: Commit**

```bash
git add backend/app/data_providers/fquant/engine_data_disk.py backend/tests/data_providers/test_engine_data_disk.py
git commit -m "feat(data): add EngineDataDiskClient.get_fund_range for three-locks capital lock"
```

---

### Task 2: `FQuantProvider.get_moneyflow_range()` — provider 层转发

**文件：**
- 修改：`backend/app/data_providers/fquant_provider.py`（紧邻既有 `get_moneyflow_daily`，约第1094-1124行之后）
- 测试：`backend/tests/data_providers/test_provider_moneyflow_disk.py`

**接口：**
- Consumes：Task 1 的 `EngineDataDiskClient.get_fund_range(code, start_iso, end_iso, asset_type=None) -> pl.DataFrame`；既有 `self._engine_key(symbol, code) -> str`（`fquant_provider.py:200-201`，磁盘模式下返回 `symbol` 本身，HTTP 模式返回 `code`）。
- Produces（Task 3 依赖）：`FQuantProvider.get_moneyflow_range(symbol: str, start: datetime, end: datetime) -> pl.DataFrame`，磁盘模式（`hasattr(self._engine, "get_fund_range")` 为真）转发到 `self._engine.get_fund_range(...)`；HTTP 模式（`hasattr` 为假）直接返回空 `pl.DataFrame()`。返回列同 Task 1：`date`/`main_net_inflow`。

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/data_providers/test_provider_moneyflow_disk.py` 末尾新增（复用文件已有的 `object.__new__(FQuantProvider)` 手动装配模式）：

```python
class FakeFundRangeEngine:
    def __init__(self, df):
        self._df = df
        self.calls = []

    def get_fund_range(self, code, start_iso, end_iso, asset_type=None):
        self.calls.append((code, start_iso, end_iso, asset_type))
        return self._df


class EngineWithoutFundRange:
    """模拟 HTTP 模式的 engine —— 没有 get_fund_range 方法。"""


def test_moneyflow_range_forwards_to_disk_engine():
    import polars as pl
    from datetime import datetime

    fake_df = pl.DataFrame({"date": ["2026-07-01"], "main_net_inflow": [300.0]})
    engine = FakeFundRangeEngine(fake_df)
    provider = object.__new__(FQuantProvider)
    provider._engine = engine
    provider._engine_mode = "disk"
    provider.name = "fquant_local"

    df = provider.get_moneyflow_range("600519.SH", datetime(2026, 6, 30), datetime(2026, 7, 1))

    assert engine.calls == [("600519.SH", "2026-06-30", "2026-07-01", None)]
    assert df.to_dicts() == [{"date": "2026-07-01", "main_net_inflow": 300.0}]


def test_moneyflow_range_returns_empty_for_http_engine():
    import polars as pl
    from datetime import datetime

    provider = object.__new__(FQuantProvider)
    provider._engine = EngineWithoutFundRange()
    provider._engine_mode = "http"
    provider.name = "fquant"

    df = provider.get_moneyflow_range("600519.SH", datetime(2026, 6, 30), datetime(2026, 7, 1))

    assert isinstance(df, pl.DataFrame)
    assert df.is_empty()
```

- [ ] **Step 2: 运行测试验证失败**

运行：`cd backend && uv run --extra dev pytest tests/data_providers/test_provider_moneyflow_disk.py -k moneyflow_range -v`
预期：2 个测试 FAIL，报 `AttributeError: 'FQuantProvider' object has no attribute 'get_moneyflow_range'`

- [ ] **Step 3: 实现 `get_moneyflow_range`**

在 `backend/app/data_providers/fquant_provider.py` 里紧跟 `get_moneyflow_daily` 方法（约第1094-1124行）之后插入：

```python
    def get_moneyflow_range(self, symbol: str, start: datetime, end: datetime) -> pl.DataFrame:
        """区间资金流查询（三锁指标资金锁专用）。

        磁盘模式：转发给 engine 的 get_fund_range（一次性整档读取，见 EngineDataDiskClient）。
        HTTP 模式：moneyflow HTTP API 无区间查询能力，直接返回空 df（资金锁相应判"数据不足"）。
        """
        if not hasattr(self._engine, "get_fund_range"):
            return pl.DataFrame()
        code = symbol_to_code(symbol)
        engine_key = self._engine_key(symbol, code)
        start_iso = start.strftime("%Y-%m-%d")
        end_iso = end.strftime("%Y-%m-%d")
        return self._engine.get_fund_range(engine_key, start_iso, end_iso)
```

确认文件顶部已 import `symbol_to_code`（`get_moneyflow_daily` 已经在用，`fquant_provider.py:1108`）。

- [ ] **Step 4: 运行测试验证通过**

运行：`cd backend && uv run --extra dev pytest tests/data_providers/test_provider_moneyflow_disk.py -v`
预期：全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/data_providers/fquant_provider.py backend/tests/data_providers/test_provider_moneyflow_disk.py
git commit -m "feat(data): add FQuantProvider.get_moneyflow_range forwarding to disk engine"
```

---

### Task 3: `kline.py` 本地模式合并主力净流入

**文件：**
- 修改：`backend/app/api/kline.py`（`is_local_daily_mode()` 分支，`compute_enriched` 调用之后，约第235-245行）
- 测试：`backend/tests/api/test_kline_local_fallback.py`

**接口：**
- Consumes：Task 2 的 `provider.get_moneyflow_range(symbol, start_dt, end_dt) -> pl.DataFrame`（列 `date`/`main_net_inflow`）；本地模式分支既有变量 `provider`/`asset_type`/`start_dt`/`end_dt`/`enriched`（`kline.py:199-258`，见下方 Step 3 的确切上下文）。
- Produces：`enriched` DataFrame（进而 `rows`/API 响应）新增 `main_net_inflow` 列，`asset_type != "stock"` 时该列恒为 `null`；供前端 Task 4 的 `threeLocks.ts` 消费。

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/api/test_kline_local_fallback.py` 末尾新增（扩展文件已有的 `FakeProvider`，加 `get_moneyflow_range` 方法）：

```python
class FakeProviderWithMoneyflow(FakeProvider):
    def __init__(self, moneyflow_df):
        super().__init__()
        self._moneyflow_df = moneyflow_df
        self.moneyflow_args = None

    def get_moneyflow_range(self, symbol, start, end):
        self.moneyflow_args = (symbol, start, end)
        return self._moneyflow_df


def test_daily_local_mode_merges_main_net_inflow_for_stock(monkeypatch):
    import polars as pl

    moneyflow_df = pl.DataFrame({
        "date": ["2026-07-01"],
        "main_net_inflow": [300.0],
    })
    provider = FakeProviderWithMoneyflow(moneyflow_df)
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-01",
        ext_columns=None,
    )

    assert provider.moneyflow_args is not None
    assert provider.moneyflow_args[0] == "600519.SH"
    assert resp["rows"][0]["main_net_inflow"] == 300.0


def test_daily_local_mode_skips_moneyflow_for_non_stock(monkeypatch):
    provider = FakeProviderWithMoneyflow(None)

    def _fail_if_called(symbol, start, end):
        raise AssertionError("get_moneyflow_range should not be called for non-stock asset types")
    provider.get_moneyflow_range = _fail_if_called

    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "02577.HK",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-02",
        ext_columns=None,
    )

    assert resp["rows"][0]["main_net_inflow"] is None


def test_daily_local_mode_main_net_inflow_null_when_moneyflow_empty(monkeypatch):
    import polars as pl

    provider = FakeProviderWithMoneyflow(pl.DataFrame())
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-01",
        ext_columns=None,
    )

    assert resp["rows"][0]["main_net_inflow"] is None


def test_daily_local_mode_moneyflow_exception_does_not_500(monkeypatch):
    provider = FakeProvider()

    def _raise(symbol, start, end):
        raise RuntimeError("disk read failed")
    provider.get_moneyflow_range = _raise

    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-01",
        ext_columns=None,
    )

    assert resp["rows"][0]["main_net_inflow"] is None
    assert resp["rows"][0]["close"] == 1.0
```

- [ ] **Step 2: 运行测试验证失败**

运行：`cd backend && uv run --extra dev pytest tests/api/test_kline_local_fallback.py -k main_net_inflow -v`
预期：4 个测试 FAIL（`main_net_inflow` 字段不存在于 `resp["rows"][0]`，报 `KeyError`）

- [ ] **Step 3: 实现合并逻辑**

修改 `backend/app/api/kline.py`，在 `is_local_daily_mode()` 分支里 `enriched = compute_enriched(...)` 这一行之后、`rows = _maybe_inject_live_candle(...)` 之前插入合并步骤：

```python
        enriched = compute_enriched(raw, factors=factors, instruments=instruments, asset_type=asset_type)
        if asset_type == "stock":
            try:
                moneyflow = provider.get_moneyflow_range(symbol, start_dt, end_dt)
            except Exception as e:  # noqa: BLE001
                logger.debug("本地模式资金流拉取失败 %s: %s", symbol, e)
                moneyflow = pl.DataFrame()
            if not moneyflow.is_empty():
                # date 列用字符串做 join key: get_fund_range (Task 1) 返回的 date 是 pl.Utf8;
                # enriched 的 date 列实际类型不确定 (可能是 pl.Date/pl.Datetime/pl.Utf8, 取决于
                # compute_enriched 内部处理), 显式转字符串做 join key 就不需要关心它原本是什么类型。
                enriched = (
                    enriched
                    .with_columns(pl.col("date").cast(pl.Utf8).alias("_date_key"))
                    .join(
                        moneyflow.rename({"date": "_date_key"}),
                        on="_date_key",
                        how="left",
                    )
                    .drop("_date_key")
                )
            else:
                enriched = enriched.with_columns(pl.lit(None).cast(pl.Float64).alias("main_net_inflow"))
        else:
            enriched = enriched.with_columns(pl.lit(None).cast(pl.Float64).alias("main_net_inflow"))
        rows = _maybe_inject_live_candle(request, symbol, enriched.tail(days).to_dicts())
```

`.cast(pl.Utf8)` 对 `pl.Date`/`pl.Datetime`/`pl.Utf8` 任意一种原始类型都能正常工作（转成 `YYYY-MM-DD` 或等价的可比较字符串），不需要提前确认 `enriched` 的 `date` 列具体是哪种类型——这就是用这个写法而不是"先查清楚类型再决定要不要转换"的原因。

- [ ] **Step 4: 运行测试验证通过**

运行：`cd backend && uv run --extra dev pytest tests/api/test_kline_local_fallback.py -v`
预期：全部 PASS（含新增 4 个 + 原有测试）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/kline.py backend/tests/api/test_kline_local_fallback.py
git commit -m "feat(api): merge main_net_inflow into local-mode kline response"
```

---

### Task 4: 前端 `threeLocks.ts` — 移植三锁算法

**文件：**
- 创建：`frontend/src/lib/threeLocks.ts`

**接口：**
- Consumes：无（纯函数模块，输入是调用方传入的 K 线数组）。
- Produces（Task 6 依赖）：
  - `interface ThreeLocksKLinePoint { date: string; high: number | null | undefined; low?: number | null | undefined; close: number | null | undefined; volume: number | null | undefined; main_net_inflow: number | null | undefined }`
  - `type LockState = boolean | null`, `type LockKey = "trend" | "capital" | "pattern"`, `type LockDirection = "on" | "off"`
  - `interface ThreeLocksResult { date: string | null; trendLocked: LockState; capitalLocked: LockState; patternLocked: LockState; totalLocked: number; ma5: number | null; ma10: number | null; ma20: number | null; ma60: number | null; capital3DaySum: number | null; capitalValidDays: number; recentHigh3: number | null; priorHigh17: number | null }`
  - `interface ThreeLockSignal { kind: "buy" | "sell"; date: string; index: number }`
  - `interface PerLockSignal { lock: LockKey; direction: LockDirection; date: string; index: number }`
  - `interface AllSignals { combined: ThreeLockSignal[]; perLock: PerLockSignal[] }`
  - `interface LockStateSnapshot { trend: boolean; capital: boolean; pattern: boolean }`
  - `interface ClusterSignal { date: string; index: number; states: LockStateSnapshot }`
  - `function sortKLinesByDateAsc<T extends { date: string }>(rows: T[]): T[]`
  - `function computeThreeLocks(rows: ThreeLocksKLinePoint[]): ThreeLocksResult`
  - `function buildAllSignals(rows: ThreeLocksKLinePoint[]): AllSignals`
  - `function buildClusterSignals(rows: ThreeLocksKLinePoint[]): ClusterSignal[]`

- [ ] **Step 1: 创建文件，逐行移植算法**

创建 `frontend/src/lib/threeLocks.ts`，内容是 `../fquant/web/src/components/threeLocks.ts` 的逐行移植（类型名、函数名、算法逻辑完全一致，只改了顶部注释说明来源）：

```typescript
// 三把锁指标（趋势锁/资金锁/形态锁），移植自 ../fquant 的 threeLocks.ts（v4，对齐"指南针"App 截图）。
// 算法逻辑与 fquant 逐行对齐，不要自行调整判定公式——详见
// docs/superpowers/specs/2026-07-05-three-locks-indicator-design.md 的"三把锁定义"章节。

export interface ThreeLocksKLinePoint {
  date: string
  high: number | null | undefined
  low?: number | null | undefined
  close: number | null | undefined
  volume: number | null | undefined
  main_net_inflow: number | null | undefined
}

export type LockState = boolean | null
export type LockKey = 'trend' | 'capital' | 'pattern'
export type LockDirection = 'on' | 'off'

export interface MovingAveragePoint {
  date: string
  value: number | null
}

export interface ThreeLocksResult {
  date: string | null
  trendLocked: LockState
  capitalLocked: LockState
  patternLocked: LockState
  totalLocked: number
  ma5: number | null
  ma10: number | null
  ma20: number | null
  ma60: number | null
  capital3DaySum: number | null
  capitalValidDays: number
  recentHigh3: number | null
  priorHigh17: number | null
}

export interface ThreeLockSignal {
  kind: 'buy' | 'sell'
  date: string
  index: number
}

export interface PerLockSignal {
  lock: LockKey
  direction: LockDirection
  date: string
  index: number
}

export interface AllSignals {
  combined: ThreeLockSignal[]
  perLock: PerLockSignal[]
}

export interface LockStateSnapshot {
  trend: boolean
  capital: boolean
  pattern: boolean
}

export interface ClusterSignal {
  date: string
  index: number
  states: LockStateSnapshot
}

const MA_PERIODS = [5, 10, 20, 60] as const
const CAPITAL_LOOKBACK_DAYS = 3

function isFiniteNumber(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function average(values: number[]): number | null {
  if (values.length === 0) return null
  return values.reduce((sum, value) => sum + value, 0) / values.length
}

function maxFinite(values: Array<number | null | undefined>): number | null {
  const finite = values.filter(isFiniteNumber)
  return finite.length === values.length && finite.length > 0 ? Math.max(...finite) : null
}

export function sortKLinesByDateAsc<T extends { date: string }>(rows: T[]): T[] {
  return rows.slice().sort((left, right) => left.date.localeCompare(right.date))
}

export function buildMovingAverageSeries(
  rows: ThreeLocksKLinePoint[],
  period: number
): MovingAveragePoint[] {
  const sorted = sortKLinesByDateAsc(rows)
  return sorted.map((row, index) => {
    if (index + 1 < period) {
      return { date: row.date, value: null }
    }
    const closes = sorted.slice(index + 1 - period, index + 1).map(item => item.close)
    if (!closes.every(isFiniteNumber)) {
      return { date: row.date, value: null }
    }
    return { date: row.date, value: average(closes as number[]) }
  })
}

export function computeThreeLocks(rows: ThreeLocksKLinePoint[]): ThreeLocksResult {
  const sorted = sortKLinesByDateAsc(rows)
  const latest = sorted[sorted.length - 1] ?? null

  const maSeries: Record<number, number | null> = {}
  for (const period of MA_PERIODS) {
    const series = buildMovingAverageSeries(sorted, period)
    maSeries[period] = series[series.length - 1]?.value ?? null
  }

  // 趋势锁: MA5 > MA10 > MA20 > MA60 (4 线多头排列)
  const trendLocked =
    maSeries[5] === null || maSeries[10] === null || maSeries[20] === null || maSeries[60] === null
      ? null
      : maSeries[5]! > maSeries[10]! &&
        maSeries[10]! > maSeries[20]! &&
        maSeries[20]! > maSeries[60]!

  // 资金锁: 最近 3 日累计主力净流入 > 0
  const last3 = sorted.slice(-CAPITAL_LOOKBACK_DAYS)
  const validInflowDays = last3.filter(item => item.main_net_inflow != null).length
  const capital3DaySum = last3.reduce((sum, item) => sum + (item.main_net_inflow ?? 0), 0)
  const capitalLocked = validInflowDays < CAPITAL_LOOKBACK_DAYS ? null : capital3DaySum > 0

  // 形态锁: close > MA20 + 近 3 日最高 > 4-20 日最高
  const latestClose = latest?.close
  const recentHigh3 = sorted.length >= 3 ? maxFinite(sorted.slice(-3).map(item => item.high)) : null
  const priorHigh17 = sorted.length >= 20 ? maxFinite(sorted.slice(-20, -3).map(item => item.high)) : null
  const patternLocked =
    maSeries[20] === null || !isFiniteNumber(latestClose) || recentHigh3 === null || priorHigh17 === null
      ? null
      : latestClose > maSeries[20]! && recentHigh3 > priorHigh17

  const states: LockState[] = [trendLocked, capitalLocked, patternLocked]
  return {
    date: latest?.date ?? null,
    trendLocked,
    capitalLocked,
    patternLocked,
    totalLocked: states.filter(state => state === true).length,
    ma5: maSeries[5],
    ma10: maSeries[10],
    ma20: maSeries[20],
    ma60: maSeries[60],
    capital3DaySum: validInflowDays === CAPITAL_LOOKBACK_DAYS ? capital3DaySum : null,
    capitalValidDays: validInflowDays,
    recentHigh3,
    priorHigh17,
  }
}

function stateOf(result: ThreeLocksResult, lock: LockKey): LockState {
  if (lock === 'trend') return result.trendLocked
  if (lock === 'capital') return result.capitalLocked
  return result.patternLocked
}

export function buildThreeLockSignals(rows: ThreeLocksKLinePoint[]): ThreeLockSignal[] {
  return buildAllSignals(rows).combined
}

// 同时返回综合买卖点(三锁首次全开/首次破开)和逐锁独立状态变化，
// 供图表渲染更丰富的标记集合(对齐"指南针"的展示方式)。
export function buildAllSignals(rows: ThreeLocksKLinePoint[]): AllSignals {
  const sorted = sortKLinesByDateAsc(rows)
  const combined: ThreeLockSignal[] = []
  const perLock: PerLockSignal[] = []

  const previous: Record<LockKey, LockState> = {
    trend: null,
    capital: null,
    pattern: null,
  }
  let previousAllOpen = false

  sorted.forEach((row, index) => {
    const result = computeThreeLocks(sorted.slice(0, index + 1))
    const allOpen =
      result.trendLocked === true &&
      result.capitalLocked === true &&
      result.patternLocked === true

    if (allOpen && !previousAllOpen) {
      combined.push({ kind: 'buy', date: row.date, index })
    }
    if (!allOpen && previousAllOpen) {
      combined.push({ kind: 'sell', date: row.date, index })
    }
    previousAllOpen = allOpen

    ;(['trend', 'capital', 'pattern'] as LockKey[]).forEach(lock => {
      const current = stateOf(result, lock)
      const prev = previous[lock]
      if (current === null) return
      if (prev === null) {
        perLock.push({ lock, direction: current ? 'on' : 'off', date: row.date, index })
        previous[lock] = current
        return
      }
      if (current !== prev) {
        perLock.push({ lock, direction: current ? 'on' : 'off', date: row.date, index })
        previous[lock] = current
      }
    })
  })

  return { combined, perLock }
}

// 在(趋势,资金,形态)三元组任一变化时出簇状信号，每次状态变化都出一个 cluster。
export function buildClusterSignals(rows: ThreeLocksKLinePoint[]): ClusterSignal[] {
  const sorted = sortKLinesByDateAsc(rows)
  const signals: ClusterSignal[] = []
  let previous: LockStateSnapshot | null = null

  sorted.forEach((row, index) => {
    const r = computeThreeLocks(sorted.slice(0, index + 1))
    if (r.trendLocked === null || r.capitalLocked === null || r.patternLocked === null) {
      return
    }
    const states: LockStateSnapshot = {
      trend: r.trendLocked,
      capital: r.capitalLocked,
      pattern: r.patternLocked,
    }
    if (
      previous === null ||
      previous.trend !== states.trend ||
      previous.capital !== states.capital ||
      previous.pattern !== states.pattern
    ) {
      signals.push({ date: row.date, index, states })
      previous = states
    }
  })

  return signals
}
```

- [ ] **Step 2: 类型检查**

运行：`cd frontend && pnpm tsc --noEmit`
预期：exit 0，无报错（这个文件此时还没被任何地方 import，纯新增不影响既有代码）

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/threeLocks.ts
git commit -m "feat(charts): port three-locks algorithm from fquant"
```

---

### Task 5: 前端 `threeLocks.test.ts` — 移植14个用例

**文件：**
- 创建：`frontend/src/lib/threeLocks.test.ts`

**接口：**
- Consumes：Task 4 的 `computeThreeLocks`/`buildAllSignals`/`buildClusterSignals`/`buildThreeLockSignals`/`sortKLinesByDateAsc`/`ThreeLocksKLinePoint`。
- Produces：无（叶子测试脚本）。

- [ ] **Step 1: 创建测试文件，移植14个用例**

创建 `frontend/src/lib/threeLocks.test.ts`，内容是 `../fquant/web/src/components/threeLocks.test.ts` 的逐行移植（只改 import 路径）：

```typescript
import {
  buildAllSignals,
  buildClusterSignals,
  buildThreeLockSignals,
  computeThreeLocks,
  sortKLinesByDateAsc,
  type ThreeLocksKLinePoint,
} from './threeLocks.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) {
    throw new Error(message)
  }
}

function makeRows(count: number): ThreeLocksKLinePoint[] {
  // 凹向上的曲线, 让短周期均线高于长周期均线 (趋势锁 = true)。
  // 纯线性上升会导致 MA5 < MA10 (最近几日值仅略高于早期值)。
  return Array.from({ length: count }, (_, index) => {
    const close = 100 + Math.pow(index / count, 1.6) * 30
    const recent = index >= count - 5
    return {
      date: `2026-01-${String(index + 1).padStart(3, '0')}`,
      high: close + (recent ? 3 : 1),
      low: close - 1,
      close,
      volume: recent ? 200 : 100,
      main_net_inflow: recent ? (index % 5 < 3 ? 10 : -2) : 1,
    }
  })
}

function withOverrides(
  rows: ThreeLocksKLinePoint[],
  fn: (row: ThreeLocksKLinePoint, index: number) => ThreeLocksKLinePoint
): ThreeLocksKLinePoint[] {
  return rows.map((row, index) => fn({ ...row }, index))
}

function testDescendingInputSortsBeforeCompute() {
  const rows = makeRows(130)
  const descending = rows.slice().reverse()
  assert(sortKLinesByDateAsc(descending)[0].date === rows[0].date, 'sort should restore ascending date order')
  assert(computeThreeLocks(descending).date === rows[rows.length - 1].date, 'latest date should come from ascending tail')
}

function testTrendNeeds60ValidCloses() {
  const result = computeThreeLocks(makeRows(59))
  assert(result.trendLocked === null, 'trend lock should be null with fewer than 60 closes (MA60 not yet ready)')
}

function testTrendAlignsWithMa5102060() {
  const result = computeThreeLocks(makeRows(130))
  assert(result.trendLocked === true, 'trend lock should be true for monotonically rising closes')
  assert(result.ma5 !== null && result.ma10 !== null && result.ma20 !== null && result.ma60 !== null, 'all 4 MAs should be computed')
}

function testCapitalIsThreeDayCumulative() {
  const result = computeThreeLocks(makeRows(130))
  assert(result.capitalLocked === true, 'capital lock should be true when 3-day cumulative inflow > 0')
  assert(result.capital3DaySum !== null && result.capital3DaySum > 0, 'capital 3-day sum should be positive')
}

function testCapitalNullWhenRecentInflowMissing() {
  const rows = withOverrides(makeRows(130), (row, index) =>
    index === 128 ? { ...row, main_net_inflow: null } : row
  )
  const result = computeThreeLocks(rows)
  assert(result.capitalLocked === null, 'capital lock should be null when a recent 3-day inflow is null')
}

function testCapitalLockedWhenAllThreeDaysNegative() {
  const rows = withOverrides(makeRows(130), (row, index) =>
    index >= 127 ? { ...row, main_net_inflow: -10 } : row
  )
  const result = computeThreeLocks(rows)
  assert(result.capitalLocked === false, 'capital lock should be false when 3-day sum is negative')
}

function testPatternExcludesRecentThreeDaysFromPriorHigh() {
  const rows = withOverrides(makeRows(130), (row, index) => {
    if (index === 126) return { ...row, high: 999 }
    if (index >= 127) return { ...row, high: 200 }
    return row
  })
  const result = computeThreeLocks(rows)
  assert(result.patternLocked === false, 'pattern lock should not compare recent highs against a baseline that includes the recent 3 days')
}

function testPatternUsesUpperShadowHigh() {
  const rows = withOverrides(makeRows(130), (row, index) => {
    if (index === 129) return { ...row, close: 129, high: 160 }
    return row
  })
  const result = computeThreeLocks(rows)
  assert(result.patternLocked === true, 'pattern lock should use high, so an upper shadow can create the 20-day high')
}

function testNullSafety() {
  const rows = withOverrides(makeRows(130), (row, index) => {
    if (index === 129) return { ...row, high: null, close: null, volume: null, main_net_inflow: null }
    return row
  })
  const result = computeThreeLocks(rows)
  assert(result.patternLocked === null, 'pattern lock should be null when latest close/high is missing')
  assert(result.capitalLocked === null, 'capital lock should be null when recent inflow is missing')
}

function testCombinedSignalDatesMapToComputedRows() {
  const rows = makeRows(130)
  const signals = buildThreeLockSignals(rows)
  assert(signals.length >= 1, 'signals should include at least the first full three-lock buy event')
  assert(signals[0].kind === 'buy', 'first three-lock signal should be buy')
}

function testPerLockTransitionsAreEmitted() {
  const rows = makeRows(130)
  const { perLock } = buildAllSignals(rows)
  const trendOns = perLock.filter(s => s.lock === 'trend' && s.direction === 'on')
  assert(trendOns.length >= 1, "per-lock trend should emit at least one 'on' transition")
}

function testTypeSafetyFixtureShape() {
  const row: ThreeLocksKLinePoint = {
    date: '2026-06-05',
    high: 12,
    low: 10,
    close: 11,
    volume: 1000,
    main_net_inflow: 200,
  }
  assert(computeThreeLocks([row]).date === row.date, 'fixture should satisfy the typed input contract')
}

function testClusterSignalsEmitOnStateChange() {
  const rows = makeRows(130)
  const clusters = buildClusterSignals(rows)
  assert(clusters.length >= 1, 'clusters should be emitted on state changes')
  const last = computeThreeLocks(rows)
  const lastCluster = clusters[clusters.length - 1]
  assert(
    lastCluster.states.trend === last.trendLocked &&
      lastCluster.states.capital === last.capitalLocked &&
      lastCluster.states.pattern === last.patternLocked,
    'last cluster should reflect current lock state'
  )
}

function testClusterSignalsCompact() {
  const rows = makeRows(130)
  const forced: ThreeLocksKLinePoint[] = rows.map(r => ({ ...r }))
  const clusters = buildClusterSignals(forced)
  assert(clusters.length < 10, 'stable state should not produce many clusters')
}

const tests = [
  testDescendingInputSortsBeforeCompute,
  testTrendNeeds60ValidCloses,
  testTrendAlignsWithMa5102060,
  testCapitalIsThreeDayCumulative,
  testCapitalNullWhenRecentInflowMissing,
  testCapitalLockedWhenAllThreeDaysNegative,
  testPatternExcludesRecentThreeDaysFromPriorHigh,
  testPatternUsesUpperShadowHigh,
  testNullSafety,
  testCombinedSignalDatesMapToComputedRows,
  testPerLockTransitionsAreEmitted,
  testTypeSafetyFixtureShape,
  testClusterSignalsEmitOnStateChange,
  testClusterSignalsCompact,
]

let failed = 0
for (const run of tests) {
  try {
    run()
    console.log(`PASS ${run.name}`)
  } catch (e) {
    failed += 1
    console.error(`FAIL ${run.name}: ${(e as Error).message}`)
  }
}
if (failed > 0) {
  console.error(`${failed}/${tests.length} tests failed`)
  process.exit(1)
}
console.log(`${tests.length}/${tests.length} tests passed`)
```

（与 fquant 原文件的唯一实质差异：原文件末尾是 `[...].forEach((run) => run())`，不检查失败、不给非零退出码；这里改成显式收集失败数量、失败时 `process.exit(1)`，这样 Step 2 能通过退出码判断测试是否全部通过，而不用人工看 console 输出。）

- [ ] **Step 2: 运行测试验证通过**

运行：`cd frontend && node src/lib/threeLocks.test.ts`
预期：打印 14 行 `PASS ...`，最后一行 `14/14 tests passed`，退出码 0。若有 `FAIL` 行，说明移植过程中引入了偏差，对照 `../fquant/web/src/components/threeLocks.ts` 逐行核对 Task 4 的实现，不要修改测试本身去迁就实现。

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/threeLocks.test.ts
git commit -m "test(charts): port three-locks 14 test cases from fquant"
```

---

### Task 6: `EChartsCandlestick.tsx` — 删除错误实现，接入新三锁可视化

**文件：**
- 修改：`frontend/src/components/EChartsCandlestick.tsx`

**接口：**
- Consumes：Task 4 的 `buildAllSignals(rows: ThreeLocksKLinePoint[]): AllSignals`（`{combined, perLock}`）。
- Produces：无（叶子可视化组件）。

- [ ] **Step 1: `OHLC` 接口新增 `main_net_inflow` 字段**

在 `frontend/src/components/EChartsCandlestick.tsx:6-31` 的 `OHLC` 接口里，紧跟 `boll_lower?: number | null` 之后新增一行：

```typescript
  boll_lower?: number | null
  main_net_inflow?: number | null
```

- [ ] **Step 2: 删除现有错误三锁实现**

删除 `buildOption` 函数体内的两处代码块：

1. 第524-570行（`// 三锁 (30/60/90 均线粘合突破...)` 整段，从 `let ma30: (number | null)[] = []` 到该 `if` 块结束的 `}`）。
2. 第731-743行（`// 三锁: MA30/60/90 三条均线本身即为...` 整段，`if (activeIndicators.includes('threelock') && ma30.length > 0) { ... }` 整个块）。

删除后，紧邻这两块的"九转"(td9) 代码块（第483-522行）和 BOLL 代码块（第717-729行）之间应该是连续的，不留空洞。

- [ ] **Step 3: 新增三锁 markPoint 生成**

在 `buildOption` 函数体内，紧跟 Step 2 删除的第一处代码块所在位置（原九转代码块 `:483-522` 之后），插入新的三锁 markPoint 生成逻辑：

```typescript
  // 三锁 (趋势/资金/形态)，移植自 ../fquant threeLocks.ts，对齐"指南针" App。
  // 逐锁独立追踪状态变化 + 综合3锁全开/破开信号，视觉规格与 td9 保持一致。
  // 必须在下方 candlestick series.push(...) 之前完成（同上方 td9 注释的原因）。
  if (activeIndicators.includes('threelock')) {
    const lockRows = data.map(d => ({
      date: d.date,
      high: d.high,
      low: d.low,
      close: d.close,
      volume: d.volume,
      main_net_inflow: d.main_net_inflow,
    }))
    const { combined, perLock } = buildAllSignals(lockRows)
    const LOCK_COLOR: Record<LockKey, string> = {
      trend: '#d23b3b',
      capital: '#c46a7a',
      pattern: '#d99930',
    }
    const LOCK_LABEL: Record<LockKey, string> = {
      trend: '趋',
      capital: '资',
      pattern: '形',
    }

    for (const s of perLock) {
      const bar = data[s.index]
      if (!bar) continue
      const color = LOCK_COLOR[s.lock]
      const isOn = s.direction === 'on'
      const coord: [string, number] = isOn ? [bar.date, bar.low] : [bar.date, bar.high]
      const offsetY = isOn ? 10 : -18
      if (compact) {
        markPointData.push({
          name: bar.date, coord,
          symbol: 'circle', symbolSize: 3, symbolOffset: [0, offsetY],
          itemStyle: { color },
          label: { show: false }, z: 100, zlevel: 10,
        })
      } else {
        markPointData.push({
          name: bar.date, coord,
          symbol: 'roundRect', symbolSize: [12, 14], symbolOffset: [0, offsetY],
          itemStyle: { color },
          label: {
            show: true, formatter: LOCK_LABEL[s.lock], color: '#fff', fontSize: 9, fontWeight: 'bold',
            fontFamily: 'JetBrains Mono, monospace',
          },
          z: 100, zlevel: 10,
        })
      }
    }

    for (const s of combined) {
      const bar = data[s.index]
      if (!bar) continue
      const isBuy = s.kind === 'buy'
      const coord: [string, number] = isBuy ? [bar.date, bar.low] : [bar.date, bar.high]
      markPointData.push({
        name: bar.date, coord,
        symbol: isBuy ? 'roundRect' : 'circle',
        symbolSize: isBuy ? [22, 22] : [18, 18],
        symbolOffset: isBuy ? [0, 20] : [0, -26],
        itemStyle: { color: isBuy ? '#F59E0B' : THEME.bear },
        label: {
          show: true, formatter: isBuy ? '锁' : '开', color: isBuy ? '#422006' : '#fff',
          fontSize: 11, fontWeight: 'bold', fontFamily: 'JetBrains Mono, monospace',
        },
        z: 100, zlevel: 10,
      })
    }
  }
```

在文件顶部 import 区新增（紧跟 `import { computeTdSequential } from '@/lib/tdSequential'` 之后）：

```typescript
import { buildAllSignals, type LockKey } from '@/lib/threeLocks'
```

- [ ] **Step 4: 类型检查**

运行：`cd frontend && pnpm tsc --noEmit`
预期：exit 0，无报错。若报 `ma30`/`ma60Lock`/`ma90`/`pctPoints` 相关的"declared but never read"或找不到符号，检查 Step 2 是否删干净（`pctPoints` 函数本身在其它地方也被用到，`:99-102`，不要删掉这个函数定义本身，只删三锁那两处引用它的代码块）。

- [ ] **Step 5: 构建验证**

运行：`cd frontend && pnpm build`
预期：成功（允许既有的 chunk-size 警告）。

- [ ] **Step 6: 手动视觉验证**

启动前端 dev 服务(如未运行)：`cd frontend && pnpm dev`。打开任意个股日K图，点击"三锁"叠加指标开关，确认：
- 不再出现旧版的黄色圆角方块"锁"标记(那是已删除的错误实现)。
- 出现新的逐锁小图标(趋势红/资金粉/形态橙)，以及三锁全开(橙色"锁"字)/破开(绿色"开"字)的综合标记。
- 关闭"三锁"开关后，图上不再有任何三锁相关标记（`activeIndicators` 不含 `'threelock'` 时整段逻辑不执行）。
- 缩小可见K线范围到超过 `COMPACT_THRESHOLD`(60根)，确认逐锁标记降级为小圆点(compact 模式)。

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/EChartsCandlestick.tsx
git commit -m "feat(charts): replace fabricated three-locks with fquant-aligned implementation"
```

---

## 手测（全部任务完成后）

```bash
# 后端: 确认新增的资金流查询链路全绿
cd backend && uv run --extra dev pytest tests/data_providers/test_engine_data_disk.py tests/data_providers/test_provider_moneyflow_disk.py tests/api/test_kline_local_fallback.py -v

# 前端: 三锁算法移植测试
cd frontend && node src/lib/threeLocks.test.ts

# 手测真实数据: 起本地服务后，打开一只本地磁盘模式覆盖的个股(如 600519.SH)日K图，
# 确认 GET /api/kline/daily?symbol=600519.SH 的响应里每行都有 main_net_inflow 字段
# (值为数字或 null，不是字段缺失)；打开"三锁"叠加指标能看到新版标记。
curl -s "http://localhost:8000/api/kline/daily?symbol=600519.SH&days=30" | python3 -c "
import json, sys
rows = json.load(sys.stdin)['rows']
print('total rows:', len(rows))
print('has main_net_inflow key:', all('main_net_inflow' in r for r in rows))
print('sample:', rows[-1].get('main_net_inflow'))
"
```

## 自检

**1. 规格覆盖度：** spec 的四层架构（Provider 层区间查询 / kline API 合并 / 前端计算单元 / 可视化）分别对应 Task 1/2、Task 3、Task 4-5、Task 6。三把锁定义（趋势/资金/形态公式、配色、逐锁事件+综合信号）在 Task 4 的移植代码和 Task 6 的可视化里体现。错误处理章节（文件不存在/部分日期缺失/磁盘异常/非stock资产类型）在 Task 1（`_read` 降级复用）、Task 3（try/except + 非stock分支）的测试里都有对应用例覆盖。测试策略章节要求的"移植14个用例"在 Task 5 完整体现。文件变更清单的6个文件（`engine_data_disk.py`/`fquant_provider.py`/`kline.py`/`threeLocks.ts`/`threeLocks.test.ts`/`EChartsCandlestick.tsx`）逐一对应 Task 1-6。覆盖完整。

**2. 占位符扫描：** 无 TBD/TODO；所有步骤均为可直接执行的完整代码，Task 4/5 的移植代码逐行来自已核实的 fquant 源文件，不是概述性描述。

**3. 类型一致性：** `ThreeLocksKLinePoint`（Task 4 定义）在 Task 6 的 `lockRows` 构造里字段名逐一对应（`date`/`high`/`low`/`close`/`volume`/`main_net_inflow`）；`main_net_inflow` 字段名在 Task 1（`get_fund_range` 返回列名）、Task 2（透传）、Task 3（`join` 后的列名）、Task 4（`ThreeLocksKLinePoint.main_net_inflow`）、Task 6（`OHLC.main_net_inflow`）五处保持一致，无命名漂移。`buildAllSignals`/`LockKey` 在 Task 6 里从 `@/lib/threeLocks` 显式 import，不重复定义。

**4. 已知限制（继承自 spec）：** 本设计只解决本地磁盘模式（`is_local_daily_mode()` 为真）这一条路径；非本地模式的两条子路径（落盘表命中 / HTTP 回退现算）均不在本次范围内，资金锁在非本地模式下恒判"数据不足"。`buildAllSignals`/`buildClusterSignals` 是 O(n²) 实现（每个索引都重新计算一次全量三锁状态），对典型 120 根K线量级（120²=14400 次基础运算）性能可忽略，不做优化。
