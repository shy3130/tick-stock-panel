# 条件选股页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建一个独立的「条件选股」页，结构化条件构建器为主、自然语言辅助填充，数据源为本地 DuckDB（enriched + financials/metrics + instruments）。

**Architecture:** 前端条件构建器产出结构化 predicate JSON；后端安全编译器把 predicate 编译为 Polars 表达式（绝不拼 SQL），在已加载的 enriched DataFrame（JOIN instruments + financials 最新报告期）上过滤。自然语言框仅调 LLM 解析成 predicate 回填构建器，永不直达执行层。

**Tech Stack:** Python 3.13 / FastAPI / Polars / DuckDB（后端）；React + TypeScript + react-router（前端）；pytest（测试）。

**Spec:** `docs/superpowers/specs/2026-07-16-condition-screener-page-design.md`

## Global Constraints

- 执行层零 SQL 字符串拼接：predicate 必须编译为 Polars 表达式；不复用 `ScreenerService.run(list[str])`。
- field / op / value 全部走白名单 + 类型校验；order_by 仅 `{白名单 field, asc|desc}`。
- 市值单位：`float_shares`/`total_shares` 单位为「股」，流通/总市值表达式须 `* shares / 1e8` 换算为亿元。
- 基本面前视偏差门控：financials JOIN 必须 `notice_date <= as_of`。
- 基本面最新报告期：按 `(report_year, quarter)` 复合排序，不仅按 quarter 字符串。
- EPS 累计口径：`basic_eps` 为累计值，派生 PE 用 TTM/年化，不用单季累计值。
- NULL 策略：数值/布尔条件遇 NULL 视为不满足（`fill_null(False)` 语义）。
- 中文注释、无 emoji。测试先行（TDD）。
- 后端新代码放 `backend/app/`，测试放 `backend/tests/`；前端放 `frontend/src/`。

---

## File Structure

**后端（新建）**
- `backend/app/services/screener_query.py` — FIELD_REGISTRY + compile_predicate + QueryService
- `backend/app/services/screener_financials.py` — financials/metrics JOIN 辅助（notice_date 门控 + 最新期 + EPS TTM + PE/PB 派生）
- `backend/app/strategy/gostock_presets.py` — go-stock 策略语句库（分级）
- `backend/app/services/nl_screener.py` — 自然语言解析（仅回填构建器）
- `backend/tests/services/test_screener_query.py`
- `backend/tests/services/test_screener_financials.py`
- `backend/tests/services/test_nl_screener.py`
- `backend/tests/strategy/test_gostock_presets.py`
- `backend/tests/api/test_condition_screener_api.py`

**后端（修改）**
- `backend/app/api/screener.py` — 加 `/query` `/nl_parse` `/fields` `/nl_presets` 端点

**前端（新建）**
- `frontend/src/pages/ConditionScreener.tsx`
- `frontend/src/components/screener/ConditionBuilder.tsx`

**前端（修改）**
- `frontend/src/lib/api.ts` — 在现有 `api` 对象加 `screenerConditionQuery/screenerNlParse/screenerFields/screenerNlPresets` + 类型
- `frontend/src/router.tsx` — 加 `condition-screener` 路由
- `frontend/src/components/Layout.tsx` — 加导航入口

---

## Task 1: FIELD_REGISTRY + compile_predicate（安全条件编译器）

**Files:**
- Create: `backend/app/services/screener_query.py`
- Test: `backend/tests/services/test_screener_query.py`

**Interfaces:**
- Produces:
  - `@dataclass FieldSpec` with fields: `key:str, group:str, label:str, source:str, value_type:str, unit:str|None, expr_col:str|None, expr_build:Callable[[],pl.Expr]|None, enum_values:list|None, available:bool`
  - `FIELD_REGISTRY: dict[str, FieldSpec]`
  - `ALLOWED_OPS: set[str]` = `{">", "<", ">=", "<=", "=", "!=", "between"}`
  - `compile_predicate(conditions: list[dict], order_by: dict|None) -> tuple[pl.Expr, tuple[str,bool]|None]`
    - `conditions` item: `{"field": str, "op": str, "value": Any}`
    - `order_by`: `{"field": str, "direction": "asc"|"desc"}` or None
    - returns `(filter_expr, (order_col, descending)|None)`; raises `ValueError` on any whitelist/type violation

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_screener_query.py
import polars as pl
import pytest
from app.services.screener_query import compile_predicate, FIELD_REGISTRY, ALLOWED_OPS


def _df():
    return pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ", "300750.SZ"],
        "close": [1500.0, 12.0, 200.0],
        "turnover_rate": [0.5, 2.0, 5.0],
        "vol_ratio_5d": [0.8, 1.5, 3.0],
        "change_pct": [0.01, 0.04, 0.09],
        "float_shares": [1_256_197_800.0, 19_405_918_198.0, 2_000_000_000.0],
        "ma5": [1490.0, 11.0, 210.0],
        "ma10": [1480.0, 11.5, 205.0],
        "ma20": [1470.0, 12.0, 200.0],
        "ma60": [1450.0, 12.5, 190.0],
        "signal_macd_golden": [True, False, True],
        "name": ["贵州茅台", "平安银行", "ST宁德"],
    })


def test_numeric_gt_condition():
    expr, order = compile_predicate([{"field": "turnover_rate", "op": ">", "value": 1.0}], None)
    out = _df().filter(expr)
    assert set(out["symbol"]) == {"000001.SZ", "300750.SZ"}
    assert order is None


def test_float_cap_unit_conversion_to_yi():
    # 流通市值 = close * float_shares / 1e8, 单位亿元
    # 茅台 1500*1.256e9/1e8 ≈ 18843 亿; 平安 12*1.94e10/1e8 ≈ 2329 亿; 宁德 200*2e9/1e8=4000 亿
    expr, _ = compile_predicate([{"field": "float_cap", "op": "<", "value": 5000}], None)
    out = _df().filter(expr)
    assert set(out["symbol"]) == {"300750.SZ"}


def test_bool_signal_condition():
    expr, _ = compile_predicate([{"field": "macd_golden", "op": "=", "value": True}], None)
    out = _df().filter(expr)
    assert set(out["symbol"]) == {"600519.SH", "300750.SZ"}


def test_bullish_alignment_condition():
    expr, _ = compile_predicate([{"field": "bullish_alignment", "op": "=", "value": True}], None)
    out = _df().filter(expr)
    assert set(out["symbol"]) == {"300750.SZ"}  # 只有宁德 ma5>ma10>ma20>ma60


def test_exclude_st():
    expr, _ = compile_predicate([{"field": "exclude_st", "op": "=", "value": True}], None)
    out = _df().filter(expr)
    assert "300750.SZ" not in set(out["symbol"])  # ST宁德 被排除


def test_order_by_direction():
    expr, order = compile_predicate(
        [{"field": "change_pct", "op": ">", "value": 0.0}],
        {"field": "change_pct", "direction": "desc"},
    )
    assert order == ("change_pct", True)


def test_reject_unknown_field():
    with pytest.raises(ValueError, match="未知字段"):
        compile_predicate([{"field": "pe_ttm_secret", "op": ">", "value": 1}], None)


def test_reject_bad_op():
    with pytest.raises(ValueError, match="非法运算符"):
        compile_predicate([{"field": "close", "op": "DROP", "value": 1}], None)


def test_reject_bad_order_direction():
    with pytest.raises(ValueError, match="方向"):
        compile_predicate([{"field": "close", "op": ">", "value": 1}],
                          {"field": "close", "direction": "; DROP"})


def test_reject_non_numeric_value():
    with pytest.raises(ValueError, match="数值"):
        compile_predicate([{"field": "close", "op": ">", "value": "abc"}], None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/services/test_screener_query.py -v`
Expected: FAIL with "ModuleNotFoundError: app.services.screener_query"

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/screener_query.py
"""条件选股安全编译器 (§ 条件选股页 spec)。

把结构化 predicate 编译为 Polars 表达式, 绝不拼 SQL 字符串。
field/op/value 全部走白名单 + 类型校验。
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Callable

import polars as pl

ALLOWED_OPS: set[str] = {">", "<", ">=", "<=", "=", "!=", "between"}
_ORDER_DIRECTIONS = {"asc": False, "desc": True}


@dataclass
class FieldSpec:
    key: str
    group: str
    label: str
    source: str            # persist | runtime | financials | derived
    value_type: str        # number | bool | enum
    unit: str | None = None
    expr_col: str | None = None                     # 简单列名
    expr_build: Callable[[], pl.Expr] | None = None  # 复杂派生表达式
    enum_values: list | None = None
    available: bool = True

    def base_expr(self) -> pl.Expr:
        if self.expr_build is not None:
            return self.expr_build()
        if self.expr_col is not None:
            return pl.col(self.expr_col)
        raise ValueError(f"字段 {self.key} 未定义表达式")


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"值必须为数值: {v!r}")


# ── 白名单字段 ──────────────────────────────────────────────
def _build_registry() -> dict[str, FieldSpec]:
    specs: list[FieldSpec] = [
        FieldSpec("change_pct", "行情", "涨跌幅(小数,0.05=5%)", "persist", "number", None, expr_col="change_pct"),
        FieldSpec("close", "行情", "股价", "persist", "number", "元", expr_col="close"),
        FieldSpec("turnover_rate", "行情", "换手率", "persist", "number", "%", expr_col="turnover_rate"),
        FieldSpec("vol_ratio_5d", "行情", "量比", "runtime", "number", "倍", expr_col="vol_ratio_5d"),
        FieldSpec("amount", "行情", "成交额", "persist", "number", "元", expr_col="amount"),
        FieldSpec("float_cap", "市值", "流通市值", "derived", "number", "亿元",
                  expr_build=lambda: pl.col("close") * pl.col("float_shares") / 1e8),
        FieldSpec("total_cap", "市值", "总市值", "derived", "number", "亿元",
                  expr_build=lambda: pl.col("close") * pl.col("total_shares") / 1e8),
        FieldSpec("rsi_14", "技术", "RSI", "runtime", "number", None, expr_col="rsi_14"),
        FieldSpec("kdj_k", "技术", "KDJ-K", "runtime", "number", None, expr_col="kdj_k"),
        FieldSpec("macd_golden", "技术", "MACD金叉", "runtime", "bool", None,
                  expr_build=lambda: pl.col("signal_macd_golden").fill_null(False)),
        FieldSpec("boll_breakout", "技术", "布林上轨突破", "runtime", "bool", None,
                  expr_build=lambda: pl.col("signal_boll_breakout_upper").fill_null(False)),
        FieldSpec("bullish_alignment", "技术", "均线多头", "runtime", "bool", None,
                  expr_build=lambda: (
                      (pl.col("ma5") > pl.col("ma10"))
                      & (pl.col("ma10") > pl.col("ma20"))
                      & (pl.col("ma20") > pl.col("ma60"))
                  ).fill_null(False)),
        FieldSpec("above_ma20", "技术", "站上MA20", "runtime", "bool", None,
                  expr_build=lambda: (pl.col("close") > pl.col("ma20")).fill_null(False)),
        FieldSpec("limit_up", "涨停", "当日涨停", "runtime", "bool", None,
                  expr_build=lambda: pl.col("signal_limit_up").fill_null(False)),
        FieldSpec("consecutive_limit_ups", "涨停", "连板数", "runtime", "number", "次",
                  expr_col="consecutive_limit_ups"),
        # 基本面 (JOIN financials, Task 2/3 提供列)
        FieldSpec("yo_y_profit", "基本面", "净利润同比", "financials", "number", "%", expr_col="yo_y_profit"),
        FieldSpec("weight_avg_roe", "基本面", "ROE", "financials", "number", "%", expr_col="weight_avg_roe"),
        FieldSpec("gross_margin", "基本面", "毛利率", "financials", "number", "%", expr_col="gross_margin"),
        FieldSpec("pe_approx", "基本面", "PE(近似)", "derived", "number", "倍", expr_col="pe_approx"),
        FieldSpec("pb_approx", "基本面", "PB(近似)", "derived", "number", "倍", expr_col="pb_approx"),
        FieldSpec("industry", "基本面", "行业", "financials", "enum", None, expr_col="industry"),
        # 过滤
        FieldSpec("board", "过滤", "板块", "derived", "enum", None,
                  enum_values=["沪主板", "深主板", "创业板", "科创板", "北交所"]),
        FieldSpec("exclude_st", "过滤", "排除ST", "derived", "bool", None,
                  expr_build=lambda: ~pl.col("name").fill_null("").str.contains(r"ST|\*ST|退")),
        # 不可用 (置灰)
        FieldSpec("main_net_inflow", "资金", "主力净流入", "financials", "number", "元", available=False),
    ]
    return {s.key: s for s in specs}


FIELD_REGISTRY: dict[str, FieldSpec] = _build_registry()

_BOARD_PREFIX = {
    "沪主板": lambda: pl.col("symbol").str.starts_with("60"),
    "深主板": lambda: pl.col("symbol").str.starts_with("00"),
    "创业板": lambda: pl.col("symbol").str.starts_with("300") | pl.col("symbol").str.starts_with("301"),
    "科创板": lambda: pl.col("symbol").str.starts_with("688"),
    "北交所": lambda: pl.col("symbol").str.contains(r"\.BJ$"),
}


def _condition_expr(cond: dict) -> pl.Expr:
    field = cond.get("field")
    op = cond.get("op")
    value = cond.get("value")
    spec = FIELD_REGISTRY.get(field)
    if spec is None:
        raise ValueError(f"未知字段: {field}")
    if not spec.available:
        raise ValueError(f"字段暂不支持: {field}")
    if op not in ALLOWED_OPS:
        raise ValueError(f"非法运算符: {op}")

    # 板块枚举
    if spec.key == "board":
        vals = value if isinstance(value, list) else [value]
        for v in vals:
            if v not in _BOARD_PREFIX:
                raise ValueError(f"未知板块: {v}")
        return pl.any_horizontal([_BOARD_PREFIX[v]() for v in vals])

    base = spec.base_expr()

    if spec.value_type == "bool":
        if op not in ("=", "!="):
            raise ValueError(f"布尔字段仅支持 =/!=: {op}")
        want = value if isinstance(value, bool) else str(value).lower() in ("true", "1")
        matched = base if want else ~base
        return ~matched if op == "!=" else matched

    if spec.value_type == "enum":
        if op not in ("=", "!="):
            raise ValueError(f"枚举字段仅支持 =/!=: {op}")
        vals = value if isinstance(value, list) else [value]
        matched = base.is_in(vals)
        return ~matched if op == "!=" else matched

    if spec.value_type == "number":
        if op == "between":
            if not (isinstance(value, (list, tuple)) and len(value) == 2):
                raise ValueError("between 需要 [lo, hi]")
            lo, hi = _num(value[0]), _num(value[1])
            return (base >= lo) & (base <= hi)
        v = _num(value)
        return {
            ">": base > v, "<": base < v, ">=": base >= v,
            "<=": base <= v, "=": base == v, "!=": base != v,
        }[op]

    raise ValueError(f"字段类型不支持条件: {spec.key}")


def compile_predicate(
    conditions: list[dict],
    order_by: dict | None,
) -> tuple[pl.Expr, tuple[str, bool] | None]:
    if not conditions:
        exprs = [pl.lit(True)]
    else:
        exprs = [_condition_expr(c) for c in conditions]
    filter_expr = pl.all_horizontal(exprs) if len(exprs) > 1 else exprs[0]

    order: tuple[str, bool] | None = None
    if order_by:
        of = order_by.get("field")
        direction = order_by.get("direction", "desc")
        spec = FIELD_REGISTRY.get(of)
        if spec is None:
            raise ValueError(f"未知排序字段: {of}")
        if direction not in _ORDER_DIRECTIONS:
            raise ValueError(f"非法排序方向: {direction}")
        order_col = spec.expr_col or spec.key
        order = (order_col, _ORDER_DIRECTIONS[direction])
    return filter_expr, order
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/services/test_screener_query.py -v`
Expected: PASS (all 10 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/screener_query.py backend/tests/services/test_screener_query.py
git commit -m "feat(screener): add safe predicate compiler (whitelist -> polars expr)"
```

---

## Task 2: financials/metrics JOIN 辅助（前视偏差门控 + EPS TTM + PE/PB 派生）

**Files:**
- Create: `backend/app/services/screener_financials.py`
- Test: `backend/tests/services/test_screener_financials.py`

**Interfaces:**
- Consumes: DuckDB parquet at `data/financials/metrics/part.parquet`（列见 spec）
- Produces:
  - `latest_financials(metrics: pl.DataFrame, as_of: date) -> pl.DataFrame`
    - 返回每 symbol 一行, 列: `symbol, yo_y_profit, weight_avg_roe, gross_margin, eps_ttm, bps, industry`
    - 只含 `notice_date <= as_of` 的报告期, 按 `(report_year, quarter)` 取最新
  - `derive_valuation(df: pl.DataFrame) -> pl.DataFrame`
    - 输入含 `close, eps_ttm, bps`, 加列 `pe_approx = close/eps_ttm`（eps_ttm<=0 → null）、`pb_approx = close/bps`（bps<=0 → null）

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_screener_financials.py
from datetime import date
import polars as pl
from app.services.screener_financials import latest_financials, derive_valuation


def _metrics():
    # 茅台 2024 四期 (累计 EPS), notice_date 递增
    return pl.DataFrame({
        "symbol": ["600519.SH"] * 4 + ["000001.SZ"],
        "report_year": ["2024", "2024", "2024", "2024", "2024"],
        "quarter": ["2024Q1", "2024Q2", "2024Q3", "2024Q4", "2024Q4"],
        "notice_date": ["2024-04-03", "2024-08-09", "2024-10-27", "2024-12-31", "2025-03-15"],
        "basic_eps": [15.34, 30.56, 45.74, 61.71, 2.07],
        "bps": [180.0, 190.0, 200.0, 210.0, 20.0],
        "yo_y_profit": [10.0, 12.0, 14.0, 15.0, None],
        "weight_avg_roe": [8.0, 16.0, 24.0, 30.0, 9.15],
        "gross_margin": [91.0, 91.5, 92.0, 92.0, None],
        "industry": ["白酒", "白酒", "白酒", "白酒", "银行"],
    })


def test_notice_date_gating_excludes_future():
    # as_of=2024-09-01: 茅台只能看到 Q1(4/3)/Q2(8/9), Q3(10/27) 未公告
    out = latest_financials(_metrics(), date(2024, 9, 1))
    row = out.filter(pl.col("symbol") == "600519.SH")
    assert row.height == 1
    # 最新可见期是 Q2, eps_ttm = 累计30.56 / 季数2 * 4 = 61.12 (基于 Q2 而非 Q3/Q4)
    assert abs(row["eps_ttm"][0] - 61.12) < 1e-6


def test_latest_period_picks_q4_when_all_visible():
    out = latest_financials(_metrics(), date(2025, 1, 1))
    row = out.filter(pl.col("symbol") == "600519.SH")
    # Q4 可见, ROE 取 30.0
    assert abs(row["weight_avg_roe"][0] - 30.0) < 1e-6


def test_pingan_not_visible_before_notice():
    # 平安 Q4 notice 2025-03-15, as_of=2025-01-01 时不可见 -> 无该 symbol 行
    out = latest_financials(_metrics(), date(2025, 1, 1))
    assert "000001.SZ" not in set(out["symbol"])


def test_derive_valuation_pe_pb():
    df = pl.DataFrame({"symbol": ["A", "B"], "close": [100.0, 50.0],
                       "eps_ttm": [5.0, 0.0], "bps": [25.0, -1.0]})
    out = derive_valuation(df)
    assert abs(out.filter(pl.col("symbol") == "A")["pe_approx"][0] - 20.0) < 1e-6
    assert out.filter(pl.col("symbol") == "B")["pe_approx"][0] is None  # eps<=0 -> null
    assert out.filter(pl.col("symbol") == "B")["pb_approx"][0] is None  # bps<=0 -> null
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/services/test_screener_financials.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/screener_financials.py
"""financials/metrics JOIN 辅助。

关键正确性约束:
  - 前视偏差门控: 只取 notice_date <= as_of 的报告期
  - 最新报告期: 按 (report_year, quarter) 复合排序
  - EPS TTM: basic_eps 为累计口径, 单季 EPS = 本期累计 - 上期累计,
    eps_ttm = 近四个单季 EPS 之和 (简化: 用最新可见累计期做年化近似)
"""
from __future__ import annotations

from datetime import date

import polars as pl

_METRIC_COLS = ["symbol", "report_year", "quarter", "notice_date",
                "basic_eps", "bps", "yo_y_profit", "weight_avg_roe", "gross_margin", "industry"]
_OUT_COLS = ["symbol", "yo_y_profit", "weight_avg_roe", "gross_margin", "eps_ttm", "bps", "industry"]


def _quarter_rank(quarter_expr: pl.Expr) -> pl.Expr:
    # '2024Q3' -> 3
    return quarter_expr.str.slice(-1).cast(pl.Int32, strict=False)


def latest_financials(metrics: pl.DataFrame, as_of: date) -> pl.DataFrame:
    if metrics.is_empty():
        return pl.DataFrame({c: [] for c in _OUT_COLS})
    df = metrics.select([c for c in _METRIC_COLS if c in metrics.columns])
    df = df.with_columns(pl.col("notice_date").str.to_date(strict=False).alias("_notice"))
    df = df.filter(pl.col("_notice") <= as_of)
    if df.is_empty():
        return pl.DataFrame({c: [] for c in _OUT_COLS})
    df = df.with_columns([
        pl.col("report_year").cast(pl.Int32, strict=False).alias("_yr"),
        _quarter_rank(pl.col("quarter")).alias("_q"),
    ])
    df = df.sort(["symbol", "_yr", "_q"])
    # 每 symbol 取最新一行
    latest = df.group_by("symbol").tail(1)
    # eps_ttm 近似: 用最新可见累计期年化 (累计 EPS / 季数 * 4)
    latest = latest.with_columns(
        pl.when(pl.col("_q") > 0)
        .then(pl.col("basic_eps") / pl.col("_q") * 4)
        .otherwise(None)
        .alias("eps_ttm")
    )
    return latest.select(_OUT_COLS)


def derive_valuation(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns([
        pl.when(pl.col("eps_ttm") > 0)
        .then(pl.col("close") / pl.col("eps_ttm"))
        .otherwise(None).alias("pe_approx"),
        pl.when(pl.col("bps") > 0)
        .then(pl.col("close") / pl.col("bps"))
        .otherwise(None).alias("pb_approx"),
    ])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/services/test_screener_financials.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/screener_financials.py backend/tests/services/test_screener_financials.py
git commit -m "feat(screener): financials join with notice_date gating and eps ttm"
```

---

## Task 3: QueryService（加载 enriched + JOIN + filter + sort + limit）

**Files:**
- Modify: `backend/app/services/screener_query.py`（追加 QueryService）
- Test: `backend/tests/services/test_screener_query.py`（追加集成测试）

**Interfaces:**
- Consumes: `ScreenerService._load_enriched_for_date`, `repo.get_instruments`, `screener_financials.latest_financials/derive_valuation`, `compile_predicate`
- Produces:
  - `@dataclass QueryResult(as_of, rows:list[dict], total:int, applied:list[dict], elapsed_ms:float)`
  - `class QueryService(repo)`：`query(as_of, conditions, order_by, limit) -> QueryResult`

- [ ] **Step 1: Write the failing test**

```python
# 追加到 backend/tests/services/test_screener_query.py
from datetime import date
from app.services.screener_query import QueryService, QueryResult


class _FakeRepo:
    def __init__(self, enriched, instruments, metrics):
        self._e = enriched; self._i = instruments; self._m = metrics
    def get_instruments(self):
        return self._i


def test_query_service_filters_and_orders(monkeypatch):
    enriched = _df().with_columns([
        pl.lit(1_256_197_800.0).alias("total_shares"),
        pl.Series("consecutive_limit_ups", [0, 0, 2]),
        pl.Series("signal_limit_up", [False, False, True]),
        pl.Series("signal_boll_breakout_upper", [False, False, True]),
        pl.Series("rsi_14", [40.0, 55.0, 70.0]),
        pl.Series("kdj_k", [30.0, 50.0, 80.0]),
        pl.Series("amount", [1e9, 2e9, 3e9]),
    ])
    instruments = pl.DataFrame({"symbol": ["600519.SH", "000001.SZ", "300750.SZ"],
                                "total_shares": [1.256e9, 1.94e10, 2e9],
                                "float_shares": [1.256e9, 1.94e10, 2e9],
                                "name": ["贵州茅台", "平安银行", "ST宁德"]})
    metrics = pl.DataFrame({"symbol": [], "report_year": [], "quarter": [],
                            "notice_date": [], "basic_eps": [], "bps": [],
                            "yo_y_profit": [], "weight_avg_roe": [], "gross_margin": [], "industry": []})

    svc = QueryService(_FakeRepo(enriched, instruments, metrics))
    monkeypatch.setattr(svc, "_load_enriched", lambda as_of: enriched)
    monkeypatch.setattr(svc, "_load_metrics", lambda: metrics)

    res = svc.query(date(2026, 7, 10),
                    conditions=[{"field": "turnover_rate", "op": ">", "value": 1.0}],
                    order_by={"field": "change_pct", "direction": "desc"}, limit=10)
    assert isinstance(res, QueryResult)
    assert [r["symbol"] for r in res.rows] == ["300750.SZ", "000001.SZ"]  # change_pct desc
    assert res.total == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/services/test_screener_query.py::test_query_service_filters_and_orders -v`
Expected: FAIL with "cannot import name 'QueryService'"

- [ ] **Step 3: Write minimal implementation (append to screener_query.py)**

```python
# 追加到 backend/app/services/screener_query.py
import time
from dataclasses import field as _dcfield
from datetime import date

from app.services.screener import ScreenerService
from app.services import screener_financials


@dataclass
class QueryResult:
    as_of: date
    rows: list[dict] = _dcfield(default_factory=list)
    total: int = 0
    applied: list[dict] = _dcfield(default_factory=list)
    elapsed_ms: float = 0.0


class QueryService:
    def __init__(self, repo) -> None:
        self.repo = repo
        self._screener = ScreenerService(repo)

    def latest_date(self) -> date | None:
        return self._screener.latest_date()

    def _load_enriched(self, as_of: date) -> pl.DataFrame:
        return self._screener._load_enriched_for_date(as_of)

    def _load_metrics(self) -> pl.DataFrame:
        import polars as _pl
        path = self.repo.store.data_dir / "financials" / "metrics" / "part.parquet"
        if not path.exists():
            return _pl.DataFrame()
        try:
            return _pl.read_parquet(path)
        except Exception:  # noqa: BLE001
            return _pl.DataFrame()

    def query(self, as_of: date, conditions: list[dict],
              order_by: dict | None = None, limit: int = 50) -> QueryResult:
        t0 = time.perf_counter()
        filter_expr, order = compile_predicate(conditions, order_by)

        df = self._load_enriched(as_of)
        if df.is_empty():
            return QueryResult(as_of=as_of, applied=conditions)

        # 确保 float_shares/total_shares/name 存在 (JOIN instruments)
        inst = self.repo.get_instruments()
        if inst is not None and not inst.is_empty():
            need = [c for c in ["symbol", "name", "float_shares", "total_shares"]
                    if c in inst.columns and (c == "symbol" or c not in df.columns)]
            if len(need) > 1:
                df = df.join(inst.select(need), on="symbol", how="left")

        # JOIN financials 最新报告期 (notice_date <= as_of) + 估值派生
        fin = screener_financials.latest_financials(self._load_metrics(), as_of)
        if not fin.is_empty():
            df = df.join(fin, on="symbol", how="left")
            df = screener_financials.derive_valuation(df)
        else:
            for c, dtype in [("yo_y_profit", pl.Float64), ("weight_avg_roe", pl.Float64),
                             ("gross_margin", pl.Float64), ("eps_ttm", pl.Float64),
                             ("bps", pl.Float64), ("pe_approx", pl.Float64),
                             ("pb_approx", pl.Float64), ("industry", pl.Utf8)]:
                if c not in df.columns:
                    df = df.with_columns(pl.lit(None).cast(dtype).alias(c))

        # 物化派生数值列, 供 order_by 按列名排序 (filter 内联表达式仍可用)
        if "float_shares" in df.columns:
            df = df.with_columns((pl.col("close") * pl.col("float_shares") / 1e8).alias("float_cap"))
        if "total_shares" in df.columns:
            df = df.with_columns((pl.col("close") * pl.col("total_shares") / 1e8).alias("total_cap"))

        try:
            df = df.filter(filter_expr)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"条件执行失败: {e}")

        total = df.height  # 匹配总数 (截断前)

        if order is not None:
            col, desc = order
            if col not in df.columns:
                raise ValueError(f"排序字段不可用: {col}")
            df = df.sort(col, descending=desc, nulls_last=True)
        if limit and limit > 0:
            df = df.head(limit)

        rows = df.to_dicts()
        for r in rows:  # sanitize NaN/inf
            for k, v in list(r.items()):
                if isinstance(v, float) and (v != v or abs(v) == float("inf")):
                    r[k] = None
        return QueryResult(as_of=as_of, rows=rows, total=total,
                           applied=conditions, elapsed_ms=(time.perf_counter() - t0) * 1000)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/services/test_screener_query.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/screener_query.py backend/tests/services/test_screener_query.py
git commit -m "feat(screener): add QueryService with enriched+financials join"
```

---

## Task 4: go-stock 策略语句库（分级）

**Files:**
- Create: `backend/app/strategy/gostock_presets.py`
- Test: `backend/tests/strategy/test_gostock_presets.py`

**Interfaces:**
- Produces: `GOSTOCK_PRESETS: list[dict]`，每条 `{id, name, description, predicate, executable_level}`
  - `predicate`: `{conditions: list[dict], order_by: dict|None}`（可直接喂 compile_predicate）
  - `executable_level`: `"full" | "needs_fundamental" | "unsupported"`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/strategy/test_gostock_presets.py
from app.strategy.gostock_presets import GOSTOCK_PRESETS
from app.services.screener_query import compile_predicate


def test_presets_structure():
    assert len(GOSTOCK_PRESETS) >= 4
    for p in GOSTOCK_PRESETS:
        assert set(p) >= {"id", "name", "description", "predicate", "executable_level"}
        assert p["executable_level"] in {"full", "needs_fundamental", "unsupported"}


def test_full_level_presets_compile():
    for p in GOSTOCK_PRESETS:
        if p["executable_level"] == "full":
            pred = p["predicate"]
            # full 级不应抛异常
            compile_predicate(pred["conditions"], pred.get("order_by"))


def test_ids_unique():
    ids = [p["id"] for p in GOSTOCK_PRESETS]
    assert len(ids) == len(set(ids))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/strategy/test_gostock_presets.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/strategy/gostock_presets.py
"""go-stock 选股策略语句库 (移植自 choice_stock_by_indicators_tool.go 例句)。

按可执行性分级:
  full             - 纯技术面/量价/市值/板块, 本地直接可跑
  needs_fundamental - 含基本面 (本地 financials 已接入, 多数可跑)
  unsupported      - 含主力资金流/实时概念, 本期不支持
"""
from __future__ import annotations

GOSTOCK_PRESETS: list[dict] = [
    {
        "id": "strong_momentum",
        "name": "强势追涨",
        "description": "涨幅2-7% + 量比>2 + 换手>3% + 站上MA20",
        "predicate": {
            "conditions": [
                {"field": "change_pct", "op": "between", "value": [0.02, 0.07]},
                {"field": "vol_ratio_5d", "op": ">", "value": 2.0},
                {"field": "turnover_rate", "op": ">", "value": 3.0},
                {"field": "above_ma20", "op": "=", "value": True},
                {"field": "exclude_st", "op": "=", "value": True},
            ],
            "order_by": {"field": "change_pct", "direction": "desc"},
        },
        "executable_level": "full",
    },
    {
        "id": "bullish_macd",
        "name": "均线多头MACD金叉",
        "description": "均线多头排列 + MACD金叉 + 非ST",
        "predicate": {
            "conditions": [
                {"field": "bullish_alignment", "op": "=", "value": True},
                {"field": "macd_golden", "op": "=", "value": True},
                {"field": "exclude_st", "op": "=", "value": True},
            ],
            "order_by": {"field": "vol_ratio_5d", "direction": "desc"},
        },
        "executable_level": "full",
    },
    {
        "id": "midcap_breakout",
        "name": "中盘突破",
        "description": "流通市值50-200亿 + 站上MA20 + 量比>1 + 换手>3%",
        "predicate": {
            "conditions": [
                {"field": "float_cap", "op": "between", "value": [50, 200]},
                {"field": "above_ma20", "op": "=", "value": True},
                {"field": "vol_ratio_5d", "op": ">", "value": 1.0},
                {"field": "turnover_rate", "op": ">", "value": 3.0},
                {"field": "exclude_st", "op": "=", "value": True},
            ],
            "order_by": {"field": "turnover_rate", "direction": "desc"},
        },
        "executable_level": "full",
    },
    {
        "id": "quality_growth",
        "name": "质优成长",
        "description": "净利润同比>50% + ROE>15% + 均线多头 (需基本面)",
        "predicate": {
            "conditions": [
                {"field": "yo_y_profit", "op": ">", "value": 50.0},
                {"field": "weight_avg_roe", "op": ">", "value": 15.0},
                {"field": "bullish_alignment", "op": "=", "value": True},
            ],
            "order_by": {"field": "yo_y_profit", "direction": "desc"},
        },
        "executable_level": "needs_fundamental",
    },
    {
        "id": "consecutive_boards",
        "name": "连板强势",
        "description": "连板≥2 + 换手>5%",
        "predicate": {
            "conditions": [
                {"field": "consecutive_limit_ups", "op": ">=", "value": 2},
                {"field": "turnover_rate", "op": ">", "value": 5.0},
            ],
            "order_by": {"field": "consecutive_limit_ups", "direction": "desc"},
        },
        "executable_level": "full",
    },
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/strategy/test_gostock_presets.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategy/gostock_presets.py backend/tests/strategy/test_gostock_presets.py
git commit -m "feat(screener): add graded go-stock preset strategy library"
```

---

## Task 5: 自然语言解析（仅回填构建器，fail-closed）

**Files:**
- Create: `backend/app/services/nl_screener.py`
- Test: `backend/tests/services/test_nl_screener.py`

**Interfaces:**
- Consumes: `app.services.ai_provider.generate_ai_text`, `FIELD_REGISTRY`
- Produces: `async parse(text: str, profile_id: str|None=None) -> dict`
  - returns `{"recognized": list[dict], "unrecognized": list[dict], "order_by": dict|None}`
  - `recognized` item: `{field, op, value}`（已校验白名单）; `unrecognized` item: `{raw, reason}`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_nl_screener.py
import json
import pytest
from app.services import nl_screener


@pytest.mark.asyncio
async def test_parse_recognizes_whitelist_and_flags_rest(monkeypatch):
    fake = json.dumps({
        "conditions": [
            {"field": "turnover_rate", "op": ">", "value": 3},
            {"field": "main_net_inflow", "op": ">", "value": 0},
            {"field": "totally_made_up", "op": "<", "value": 1},
        ],
        "order_by": {"field": "change_pct", "direction": "desc"},
    })

    async def _fake_llm(messages, **kw):
        return fake
    monkeypatch.setattr(nl_screener, "generate_ai_text", _fake_llm)

    res = await nl_screener.parse("换手率大于3 主力净流入为正 神秘条件")
    rec_fields = {c["field"] for c in res["recognized"]}
    unrec = {u["raw"] for u in res["unrecognized"]}
    assert rec_fields == {"turnover_rate"}
    assert "main_net_inflow" in "".join(unrec) or any("main_net_inflow" in u["raw"] for u in res["unrecognized"])
    assert any("totally_made_up" in u["raw"] for u in res["unrecognized"])
    assert res["order_by"] == {"field": "change_pct", "direction": "desc"}


@pytest.mark.asyncio
async def test_parse_invalid_json_retries_then_empty(monkeypatch):
    async def _bad_llm(messages, **kw):
        return "not json at all"
    monkeypatch.setattr(nl_screener, "generate_ai_text", _bad_llm)
    res = await nl_screener.parse("随便")
    assert res["recognized"] == []
    assert res.get("error")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/services/test_nl_screener.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/nl_screener.py
"""自然语言 -> 结构化 predicate 解析 (仅回填构建器, 不执行)。

安全: LLM 输出只用于回填前端构建器, 用户可见可改, 永不直达执行层。
fail-closed: 未命中白名单的条件进 unrecognized, 不静默丢弃。
"""
from __future__ import annotations

import json
import logging

from app.services.ai_provider import generate_ai_text
from app.services.screener_query import FIELD_REGISTRY, ALLOWED_OPS

logger = logging.getLogger(__name__)


def _field_catalog() -> str:
    lines = []
    for spec in FIELD_REGISTRY.values():
        if not spec.available:
            continue
        unit = f"({spec.unit})" if spec.unit else ""
        lines.append(f"- {spec.key} [{spec.group}] {spec.label}{unit} 类型={spec.value_type}")
    return "\n".join(lines)


def _system_prompt() -> str:
    return (
        "你是A股选股条件解析器。把用户的自然语言选股需求解析成严格 JSON。\n"
        "只允许使用以下白名单字段(未列出的字段也照实输出, 由后端标记为不支持):\n"
        f"{_field_catalog()}\n\n"
        "输出格式(只输出 JSON, 不要解释):\n"
        '{"conditions":[{"field":"字段key","op":">","value":数值或布尔或[lo,hi]}],'
        '"order_by":{"field":"字段key","direction":"desc"}}\n'
        "运算符只用: > < >= <= = != between in。布尔字段 value 用 true/false。"
    )


def _extract_json(text: str) -> dict | None:
    t = text.strip()
    if "```" in t:
        t = t.split("```", 1)[1]
        if t.startswith("json"):
            t = t[4:]
        t = t.split("```", 1)[0]
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None


def _classify(conditions: list[dict]) -> tuple[list[dict], list[dict]]:
    recognized, unrecognized = [], []
    for c in conditions:
        field = c.get("field")
        op = c.get("op")
        spec = FIELD_REGISTRY.get(field)
        if spec is None:
            unrecognized.append({"raw": json.dumps(c, ensure_ascii=False), "reason": f"未知字段 {field}"})
        elif not spec.available:
            unrecognized.append({"raw": json.dumps(c, ensure_ascii=False), "reason": f"{spec.label} 本地暂不支持"})
        elif op not in ALLOWED_OPS:
            unrecognized.append({"raw": json.dumps(c, ensure_ascii=False), "reason": f"非法运算符 {op}"})
        else:
            recognized.append({"field": field, "op": op, "value": c.get("value")})
    return recognized, unrecognized


async def parse(text: str, profile_id: str | None = None) -> dict:
    messages = [{"role": "system", "content": _system_prompt()},
                {"role": "user", "content": text}]
    parsed = None
    for _ in range(2):  # 一次重试
        try:
            raw = await generate_ai_text(messages, profile_id=profile_id, temperature=0.1, max_tokens=1500)
        except Exception as e:  # noqa: BLE001
            logger.warning("nl_screener LLM 调用失败: %s", e)
            return {"recognized": [], "unrecognized": [], "order_by": None, "error": str(e)}
        parsed = _extract_json(raw)
        if parsed is not None:
            break
    if parsed is None:
        return {"recognized": [], "unrecognized": [], "order_by": None, "error": "无法解析为结构化条件"}

    recognized, unrecognized = _classify(parsed.get("conditions") or [])
    order_by = parsed.get("order_by")
    if order_by and FIELD_REGISTRY.get(order_by.get("field")) is None:
        order_by = None
    return {"recognized": recognized, "unrecognized": unrecognized, "order_by": order_by}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/services/test_nl_screener.py -v`
Expected: PASS (2 tests)

Note: 若无 `pytest-asyncio`, 在 `backend/pyproject.toml` 或 `pytest.ini` 确认 `asyncio_mode = auto`（现有异步测试已依赖，先 grep 确认；缺失则加）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/nl_screener.py backend/tests/services/test_nl_screener.py
git commit -m "feat(screener): add fail-closed NL parser for condition builder"
```

---

## Task 6: API 端点（/query /nl_parse /fields /nl_presets）

**Files:**
- Modify: `backend/app/api/screener.py`
- Test: `backend/tests/api/test_condition_screener_api.py`

**Interfaces:**
- Consumes: `QueryService`, `nl_screener.parse`, `FIELD_REGISTRY`, `GOSTOCK_PRESETS`
- Produces HTTP:
  - `POST /api/screener/query` body `{conditions, order_by?, limit?, as_of?}` → `{as_of, rows, total, applied, elapsed_ms}`
  - `POST /api/screener/nl_parse` body `{text}` → `{recognized, unrecognized, order_by}`
  - `GET /api/screener/fields` → `{groups: [{group, fields:[{key,label,value_type,unit,available,enum_values}]}]}`
  - `GET /api/screener/nl_presets` → `{presets: GOSTOCK_PRESETS}`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/api/test_condition_screener_api.py
# 沿用现有 api 测试模式 (见 tests/api/test_optimize_endpoint.py:37-41):
# mini FastAPI + include_router + app.state.repo = 假 repo。
from datetime import date
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.screener import router


class _FakeRepo:
    def enriched_latest_date(self):
        return date(2026, 7, 10)
    def get_instruments(self):
        import polars as pl
        return pl.DataFrame()
    class _Store:
        from pathlib import Path
        data_dir = Path("/nonexistent")
    store = _Store()


def _client():
    app = FastAPI()
    app.include_router(router)
    app.state.repo = _FakeRepo()
    return TestClient(app)


def test_fields_endpoint():
    r = _client().get("/api/screener/fields")
    assert r.status_code == 200
    data = r.json()
    keys = {f["key"] for g in data["groups"] for f in g["fields"]}
    assert "turnover_rate" in keys and "float_cap" in keys


def test_nl_presets_endpoint():
    r = _client().get("/api/screener/nl_presets")
    assert r.status_code == 200
    assert len(r.json()["presets"]) >= 4


def test_query_rejects_bad_field():
    # compile_predicate 在加载数据前先校验白名单 -> 未知字段返回 400
    r = _client().post("/api/screener/query",
                       json={"conditions": [{"field": "evil", "op": ">", "value": 1}],
                             "as_of": "2026-07-10"})
    assert r.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/api/test_condition_screener_api.py -v`
Expected: FAIL (404 / endpoints missing)

- [ ] **Step 3: Write minimal implementation (append to api/screener.py)**

```python
# 追加到 backend/app/api/screener.py
from app.services.screener_query import QueryService, FIELD_REGISTRY
from app.services import nl_screener as _nl_screener
from app.strategy.gostock_presets import GOSTOCK_PRESETS


class QueryRequest(BaseModel):
    conditions: list[dict]
    order_by: Optional[dict] = None
    limit: int = 50
    as_of: Optional[date] = None


class NLParseRequest(BaseModel):
    text: str
    profile_id: Optional[str] = None


@router.post("/query")
def condition_query(req: QueryRequest, request: Request):
    repo = request.app.state.repo
    svc = QueryService(repo)
    as_of = req.as_of or svc.latest_date()
    if not as_of:
        raise HTTPException(status_code=400, detail="无可用数据日期")
    try:
        result = svc.query(as_of=as_of, conditions=req.conditions,
                           order_by=req.order_by, limit=req.limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _safe(asdict(result))


@router.post("/nl_parse")
async def condition_nl_parse(req: NLParseRequest):
    return await _nl_screener.parse(req.text, profile_id=req.profile_id)


@router.get("/fields")
def condition_fields():
    groups: dict[str, list] = {}
    for spec in FIELD_REGISTRY.values():
        groups.setdefault(spec.group, []).append({
            "key": spec.key, "label": spec.label, "value_type": spec.value_type,
            "unit": spec.unit, "available": spec.available, "enum_values": spec.enum_values,
        })
    return {"groups": [{"group": g, "fields": fs} for g, fs in groups.items()]}


@router.get("/nl_presets")
def condition_nl_presets():
    return {"presets": GOSTOCK_PRESETS}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/api/test_condition_screener_api.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/screener.py backend/tests/api/test_condition_screener_api.py
git commit -m "feat(screener): add condition query/nl_parse/fields/nl_presets endpoints"
```

---

## Task 7: 前端 API 客户端 + 类型

**Files:**
- Modify: `frontend/src/lib/api.ts`

**Interfaces:**
- 真实现状（已核对 api.ts）：导出的是 `export const api = {...}` 扁平方法对象 + `async function request<T>(path, init?)` helper（api.ts:11）；**不存在** `screenerApi` 对象，也**没有** `ScreenerRow` 类型。方法风格见 `api.screenerRunCustom`（api.ts:1521）。
- Produces（加到 `api` 对象 + 导出类型）：
  - `type Condition = { field: string; op: string; value: any }`
  - `type OrderBy = { field: string; direction: 'asc' | 'desc' }`
  - `type FieldMeta = {...}`、`type FieldGroup = {...}`、`type NLParseResult = {...}`、`type ConditionQueryResult = {...}`
  - `api.screenerConditionQuery(conditions, orderBy?, limit?)`、`api.screenerNlParse(text)`、`api.screenerFields()`、`api.screenerNlPresets()`

- [ ] **Step 1: Add types (edit api.ts 类型区)**

```typescript
export type Condition = { field: string; op: string; value: unknown }
export type OrderBy = { field: string; direction: 'asc' | 'desc' }
export type FieldMeta = { key: string; label: string; value_type: string; unit: string | null; available: boolean; enum_values: unknown[] | null }
export type FieldGroup = { group: string; fields: FieldMeta[] }
export type NLParseResult = { recognized: Condition[]; unrecognized: { raw: string; reason: string }[]; order_by: OrderBy | null; error?: string }
export type ConditionQueryResult = { as_of: string; rows: Record<string, unknown>[]; total: number; applied: Condition[]; elapsed_ms: number }
export type GostockPreset = { id: string; name: string; description: string; predicate: { conditions: Condition[]; order_by: OrderBy | null }; executable_level: string }
```

- [ ] **Step 2: Add methods inside the `api` object (沿用 request<T> helper)**

```typescript
  screenerConditionQuery: (conditions: Condition[], orderBy?: OrderBy, limit = 100) =>
    request<ConditionQueryResult>('/api/screener/query', {
      method: 'POST',
      body: JSON.stringify({ conditions, order_by: orderBy ?? null, limit }),
    }),
  screenerNlParse: (text: string) =>
    request<NLParseResult>('/api/screener/nl_parse', {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),
  screenerFields: () => request<{ groups: FieldGroup[] }>('/api/screener/fields'),
  screenerNlPresets: () => request<{ presets: GostockPreset[] }>('/api/screener/nl_presets'),
```

- [ ] **Step 3: Typecheck**

Run: `cd frontend && pnpm tsc --noEmit`（或 `npx tsc --noEmit`）
Expected: 无新增类型错误

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/api.ts
git commit -m "feat(screener): add condition-screener API client methods"
```

---

## Task 8: ConditionBuilder 组件

**Files:**
- Create: `frontend/src/components/screener/ConditionBuilder.tsx`

**Interfaces:**
- Consumes: `api.screenerFields`, types `Condition/OrderBy/FieldGroup/FieldMeta`
- Produces: `<ConditionBuilder value={Condition[]} onChange={(c:Condition[])=>void} />`
  - 内部拉 `api.screenerFields()` 渲染分组下拉；每行 = 字段选择 + 运算符 + 值输入；不可用字段置灰

- [ ] **Step 1: Write the component**

```tsx
// frontend/src/components/screener/ConditionBuilder.tsx
import { useEffect, useState } from 'react'
import { api, type Condition, type FieldGroup, type FieldMeta } from '@/lib/api'

const NUM_OPS = ['>', '<', '>=', '<=', '=', '!=', 'between']
const BOOL_OPS = ['=']

export function ConditionBuilder({ value, onChange }: { value: Condition[]; onChange: (c: Condition[]) => void }) {
  const [groups, setGroups] = useState<FieldGroup[]>([])
  const flat: FieldMeta[] = groups.flatMap((g) => g.fields)

  useEffect(() => { api.screenerFields().then((r) => setGroups(r.groups)) }, [])

  const specOf = (key: string) => flat.find((f) => f.key === key)

  const addRow = () => {
    const first = flat.find((f) => f.available)
    if (first) onChange([...value, { field: first.key, op: first.value_type === 'bool' ? '=' : '>', value: first.value_type === 'bool' ? true : 0 }])
  }
  const update = (i: number, patch: Partial<Condition>) => {
    const next = value.slice(); next[i] = { ...next[i], ...patch }; onChange(next)
  }
  const remove = (i: number) => onChange(value.filter((_, j) => j !== i))

  return (
    <div className="space-y-2">
      {value.map((c, i) => {
        const spec = specOf(c.field)
        const ops = spec?.value_type === 'bool' ? BOOL_OPS : NUM_OPS
        return (
          <div key={i} className="flex items-center gap-2">
            <select className="border rounded px-2 py-1 text-sm bg-transparent"
                    value={c.field} onChange={(e) => update(i, { field: e.target.value })}>
              {groups.map((g) => (
                <optgroup key={g.group} label={g.group}>
                  {g.fields.map((f) => (
                    <option key={f.key} value={f.key} disabled={!f.available}>
                      {f.label}{f.unit ? `(${f.unit})` : ''}{f.available ? '' : ' [暂不支持]'}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <select className="border rounded px-2 py-1 text-sm bg-transparent"
                    value={c.op} onChange={(e) => update(i, { op: e.target.value })}>
              {ops.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
            {spec?.value_type === 'bool' ? (
              <select className="border rounded px-2 py-1 text-sm bg-transparent"
                      value={String(c.value)} onChange={(e) => update(i, { value: e.target.value === 'true' })}>
                <option value="true">是</option><option value="false">否</option>
              </select>
            ) : c.op === 'between' ? (
              <>
                <input className="border rounded px-2 py-1 text-sm w-20 bg-transparent" type="number"
                       value={Array.isArray(c.value) ? c.value[0] : 0}
                       onChange={(e) => update(i, { value: [Number(e.target.value), Array.isArray(c.value) ? c.value[1] : 0] })} />
                <span>~</span>
                <input className="border rounded px-2 py-1 text-sm w-20 bg-transparent" type="number"
                       value={Array.isArray(c.value) ? c.value[1] : 0}
                       onChange={(e) => update(i, { value: [Array.isArray(c.value) ? c.value[0] : 0, Number(e.target.value)] })} />
              </>
            ) : (
              <input className="border rounded px-2 py-1 text-sm w-24 bg-transparent" type="number"
                     value={typeof c.value === 'number' ? c.value : 0}
                     onChange={(e) => update(i, { value: Number(e.target.value) })} />
            )}
            <button className="text-red-500 text-sm" onClick={() => remove(i)}>删除</button>
          </div>
        )
      })}
      <button className="text-sm text-blue-600" onClick={addRow}>+ 添加条件</button>
    </div>
  )
}
```

- [ ] **Step 2: Typecheck**

Run: `cd frontend && pnpm tsc --noEmit`
Expected: 无新增类型错误

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/screener/ConditionBuilder.tsx
git commit -m "feat(screener): add grouped condition builder component"
```

---

## Task 9: ConditionScreener 页面 + NL 框 + 路由 + 导航

**Files:**
- Create: `frontend/src/pages/ConditionScreener.tsx`
- Modify: `frontend/src/router.tsx`（加路由）
- Modify: `frontend/src/components/Layout.tsx`（加导航入口）

**Interfaces:**
- Consumes: `ConditionBuilder`, `api.screenerConditionQuery/screenerNlParse/screenerNlPresets`
- **不复用 `ScreenerTable`**：其 props 极重（`columns/strategyIdToName/symbolStrategyMap/onPreview/onToggleWatchlist/...`，见 ScreenerTable.tsx:19-37），与本页数据模型不符。本页用内联简表渲染 `Record<string, unknown>[]`。

- [ ] **Step 1: Write the page**

```tsx
// frontend/src/pages/ConditionScreener.tsx
import { useEffect, useState } from 'react'
import { api, type Condition, type OrderBy, type GostockPreset } from '@/lib/api'
import { ConditionBuilder } from '@/components/screener/ConditionBuilder'
import { SectionTitle } from '@/components/data/SectionTitle'

type Row = Record<string, unknown>
const COLS: { key: string; label: string }[] = [
  { key: 'symbol', label: '代码' }, { key: 'name', label: '名称' },
  { key: 'close', label: '收盘' }, { key: 'change_pct', label: '涨跌' },
  { key: 'turnover_rate', label: '换手' }, { key: 'vol_ratio_5d', label: '量比' },
  { key: 'float_cap', label: '流通市值(亿)' },
]

export function ConditionScreener() {
  const [conditions, setConditions] = useState<Condition[]>([])
  const [orderBy, setOrderBy] = useState<OrderBy | undefined>(undefined)
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [elapsed, setElapsed] = useState(0)
  const [nlText, setNlText] = useState('')
  const [ignored, setIgnored] = useState<{ raw: string; reason: string }[]>([])
  const [presets, setPresets] = useState<GostockPreset[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => { api.screenerNlPresets().then((r) => setPresets(r.presets || [])) }, [])

  const run = async () => {
    setLoading(true); setError(null)
    try {
      const res = await api.screenerConditionQuery(conditions, orderBy, 100)
      setRows(res.rows || []); setTotal(res.total || 0); setElapsed(res.elapsed_ms || 0)
    } catch (e) { setError(String(e)) } finally { setLoading(false) }
  }

  const applyNL = async () => {
    if (!nlText.trim()) return
    setLoading(true); setError(null)
    try {
      const res = await api.screenerNlParse(nlText)
      setConditions(res.recognized || [])
      setOrderBy(res.order_by || undefined)
      setIgnored(res.unrecognized || [])
    } catch (e) { setError(String(e)) } finally { setLoading(false) }
  }

  const applyPreset = (p: GostockPreset) => {
    setConditions(p.predicate.conditions || [])
    setOrderBy(p.predicate.order_by || undefined)
    setIgnored([])
  }

  const fmt = (v: unknown) => typeof v === 'number' ? v.toFixed(2) : (v == null ? '' : String(v))

  return (
    <div className="space-y-4">
      <SectionTitle title="条件选股" subtitle={`${total} 只 · ${elapsed.toFixed(0)}ms`} />

      <div className="flex gap-2">
        <input className="flex-1 border rounded px-3 py-2 text-sm bg-transparent"
               placeholder="用自然语言描述, 例: 换手率大于3 量比大于2 均线多头 非ST"
               value={nlText} onChange={(e) => setNlText(e.target.value)}
               onKeyDown={(e) => { if (e.key === 'Enter') applyNL() }} />
        <button className="px-3 py-2 rounded bg-blue-600 text-white text-sm" onClick={applyNL}>解析填充</button>
      </div>

      {ignored.length > 0 && (
        <div className="text-amber-600 text-sm">
          已忽略 {ignored.length} 个未识别条件: {ignored.map((u) => u.reason).join('; ')}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {presets.map((p) => (
          <button key={p.id} onClick={() => applyPreset(p)}
                  className="px-3 py-1.5 rounded text-sm bg-gray-200 dark:bg-gray-700"
                  title={p.description}>
            {p.name}{p.executable_level !== 'full' ? ' *' : ''}
          </button>
        ))}
      </div>

      <ConditionBuilder value={conditions} onChange={setConditions} />

      <button className="px-4 py-2 rounded bg-green-600 text-white text-sm" onClick={run} disabled={loading}>
        {loading ? '选股中...' : '执行选股'}
      </button>

      {error && <div className="text-red-500 text-sm">{error}</div>}
      {rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead><tr>{COLS.map((c) => <th key={c.key} className="px-2 py-1 text-left">{c.label}</th>)}</tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className="border-t border-gray-200 dark:border-gray-700">
                  {COLS.map((c) => <td key={c.key} className="px-2 py-1">{fmt(r[c.key])}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 2: Add route (edit router.tsx)**

在 `{ path: 'screener', element: <Screener /> },`（router.tsx:77）下一行追加：

```tsx
      { path: 'condition-screener', element: <ConditionScreener /> },
```

并在顶部 import：`import { ConditionScreener } from './pages/ConditionScreener'`

- [ ] **Step 3: Add nav entry (edit Layout.tsx)**

Run: `grep -n "screener\|选股器\|to=\|NavLink\|navItems" frontend/src/components/Layout.tsx | head`
在「选股器」导航项旁按同样结构追加一条指向 `/condition-screener`、标题「条件选股」的入口（沿用现有导航项写法）。

- [ ] **Step 4: Typecheck + build**

Run: `cd frontend && pnpm tsc --noEmit && pnpm build`
Expected: 构建成功

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/ConditionScreener.tsx frontend/src/router.tsx frontend/src/components/Layout.tsx
git commit -m "feat(screener): add condition-screener page with NL box and preset library"
```

---

## Task 10: 端到端验证

**Files:** 无新增（手动 + 现有测试）

- [ ] **Step 1: Run full backend test suite for new modules**

Run: `cd backend && python -m pytest tests/services/test_screener_query.py tests/services/test_screener_financials.py tests/services/test_nl_screener.py tests/strategy/test_gostock_presets.py tests/api/test_condition_screener_api.py -v`
Expected: 全部 PASS

- [ ] **Step 2: Drive the real app (use `run` / `verify` skill)**

启动后端 + 前端，访问「条件选股」页：
1. 点击一个 `full` 级策略（如「强势追涨」）→ 执行 → 有结果表
2. 自然语言输入「换手率大于3 量比大于2 非ST」→ 解析填充 → 条件出现在构建器 → 执行 → 有结果
3. 自然语言含「主力净流入为正」→ 出现「已忽略」提示（fail-closed 验证）
4. 手动构建「流通市值 between 50 200」→ 执行 → 结果市值在区间内（单位换算验证）

- [ ] **Step 3: Final commit (if any doc/tweak)**

```bash
git add -A && git commit -m "test(screener): e2e verification of condition screener"
```

---

## Self-Review 结果

- **Spec 覆盖**：结构化编译器(T1)、financials JOIN+前视门控+EPS TTM(T2)、QueryService(T3)、策略库分级(T4)、NL fail-closed(T5)、API(T6)、前端 API/构建器/页面/路由/导航(T7-9)、e2e(T10)。spec 全部要点有对应任务。
- **Placeholder 扫描**：无 TBD/TODO；每个 code step 有完整代码。前端不复用 `ScreenerTable`（props 过重），改内联简表；`Layout.tsx` 导航入口处标注 grep 对齐（依赖现有导航项写法）。
- **类型一致**：`Condition{field,op,value}`、`OrderBy{field,direction}`、`compile_predicate(conditions, order_by)`、`QueryService.query(as_of, conditions, order_by, limit)`、`nl_screener.parse(text, profile_id)` 在前后端各任务间一致。

## Fable 评审吸收记录（2026-07-16）

已按 Fable 模型评审（并经主 agent 实测核实）修订：
- P0 列名：`yoy_profit` 全空 → 全面改用 `yo_y_profit`；补 `industry`（满值）进白名单与 financials 输出。
- P1 前端假设错：无 `screenerApi`/`ScreenerRow` → 改用 `api` 扁平对象 + `request<T>`；`ScreenerTable` props 过重 → 改内联简表。
- P1 API 测试入口：`main.py` 无 `create_app` → 改 mini FastAPI + `include_router` + 假 repo（对齐 test_optimize_endpoint.py）。
- P1 单位：`change_pct` 为小数（0.05=5%），去掉 `%` 换算歧义。
- P1 bool/enum 分支尊重 `op`（`=`/`!=`）；`bullish_alignment`/`above_ma20` 加 `.fill_null(False)`。
- P1 `in` 未实现 → 从 `ALLOWED_OPS` 移除（board/enum 用专门分支 + is_in）。
- P1 order_by 派生列静默失效 → QueryService 物化 `float_cap`/`total_cap`，排序列缺失时 fail-closed 报错。
- P2 `total` 语义 → 截断前取 `df.height`（匹配总数）。
- P2 EPS TTM 测试加数值断言（61.12）锁行为。
- 保留近似（EPS TTM = 累计/季数×4）并注明，标准 TTM 列为后续增量。
