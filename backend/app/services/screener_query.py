"""Registry-driven, literal-only screener queries.

The legacy screener still owns ``POST /run``.  This module is the small,
typed query path used by the new API; it deliberately never accepts SQL or
Polars expressions from a request.
"""
from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class ScreenerSemanticError(ValueError):
    """A well-formed request whose field/operator/value is not supported."""

    def __init__(self, location: str, reason: str) -> None:
        self.location = location
        self.reason = reason
        super().__init__(reason)


class ScreenerDataUnavailableError(RuntimeError):
    """Required data was absent or unreadable (without leaking a path)."""

    def __init__(self, fields: list[str]) -> None:
        self.fields = list(dict.fromkeys(fields))
        super().__init__("screener data unavailable")


# Short compatibility name for callers that used the initial draft.
ScreenerDataUnavailable = ScreenerDataUnavailableError


class QueryCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    op: str
    value: Any


class QueryOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = "change_pct"
    direction: Literal["asc", "desc"] = "desc"


class ScreenerQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conditions: list[QueryCondition] = Field(min_length=1, max_length=20)
    as_of: date | None = None
    order_by: QueryOrder | None = None
    limit: StrictInt = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def validate_value_shapes(self) -> ScreenerQueryRequest:
        # These are structural errors, intentionally left to Pydantic (422).
        for i, cond in enumerate(self.conditions):
            if cond.op == "between" and (not isinstance(cond.value, list) or len(cond.value) != 2):
                raise ValueError(f"conditions[{i}].value must contain exactly two values")
            if cond.op == "in" and (not isinstance(cond.value, list) or not 1 <= len(cond.value) <= 50):
                raise ValueError(f"conditions[{i}].value must contain 1..50 values")
        return self


@dataclass(frozen=True)
class FieldSpec:
    field: str
    label: str
    group: str
    source: str
    unit: str | None
    value_type: str
    null_policy: str
    availability: str
    ops: tuple[str, ...]
    sortable: bool
    options: tuple[str, ...] | None = None
    deps: tuple[str, ...] = ()
    materialize: Callable[[pl.DataFrame], pl.Expr] | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "label": self.label,
            "group": self.group,
            "source": self.source,
            "unit": self.unit,
            "value_type": self.value_type,
            "null_policy": self.null_policy,
            "availability": self.availability,
            "ops": list(self.ops),
            "sortable": self.sortable,
            "options": (
                [{"value": value, "label": _BOARD_LABELS.get(value, value)} for value in self.options]
                if self.options is not None
                else None
            ),
        }


NUMERIC_OPS = (">", "<", ">=", "<=", "=", "!=", "between", "in")
ENUM_OPS = ("=", "!=", "in")
BOOL_OPS = ("=", "!=")
ALLOWED_OPS = set(NUMERIC_OPS)


def _spec(
    field: str,
    label: str,
    group: str,
    source: str,
    value_type: str,
    ops: tuple[str, ...] = NUMERIC_OPS,
    *,
    unit: str | None = None,
    null_policy: str = "no_match",
    sortable: bool = True,
    options: tuple[str, ...] | None = None,
    deps: tuple[str, ...] = (),
    materialize: Callable[[pl.DataFrame], pl.Expr] | None = None,
) -> FieldSpec:
    value_type = {"decimal": "numeric", "bool": "boolean"}.get(value_type, value_type)
    return FieldSpec(field, label, group, source, unit, value_type, null_policy, "available", ops, sortable, options, deps, materialize)


_BOARD_OPTIONS = ("sh_main", "sz_main", "chinext", "star", "bse")
_BOARD_LABELS = {
    "sh_main": "沪市主板",
    "sz_main": "深市主板",
    "chinext": "创业板",
    "star": "科创板",
    "bse": "北交所",
}


FIELD_REGISTRY: dict[str, FieldSpec] = {
    "change_pct": _spec("change_pct", "涨跌幅", "market", "persist/runtime", "decimal", unit="小数"),
    "close": _spec("close", "收盘价", "market", "persist", "decimal", unit="元"),
    "turnover_rate": _spec("turnover_rate", "换手率", "market", "persist", "decimal", unit="%"),
    "vol_ratio_5d": _spec("vol_ratio_5d", "5日量比", "market", "runtime", "decimal", unit="倍"),
    "amount": _spec("amount", "成交额", "market", "persist", "decimal", unit="元"),
    "float_market_cap": _spec("float_market_cap", "流通市值", "market_cap", "derived", "decimal", unit="亿元", deps=("close", "float_shares")),
    "total_market_cap": _spec("total_market_cap", "总市值", "market_cap", "derived", "decimal", unit="亿元", deps=("close", "total_shares")),
    "ma_bullish_alignment": _spec("ma_bullish_alignment", "均线多头排列", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma5", "ma10", "ma20", "ma60")),
    "above_ma5": _spec("above_ma5", "站上MA5", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma5")),
    "above_ma10": _spec("above_ma10", "站上MA10", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma10")),
    "above_ma20": _spec("above_ma20", "站上MA20", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma20")),
    "above_ma60": _spec("above_ma60", "站上MA60", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma60")),
    "below_ma5": _spec("below_ma5", "跌破MA5", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma5")),
    "below_ma10": _spec("below_ma10", "跌破MA10", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma10")),
    "below_ma20": _spec("below_ma20", "跌破MA20", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma20")),
    "below_ma60": _spec("below_ma60", "跌破MA60", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma60")),
    "macd_golden": _spec("macd_golden", "MACD金叉", "technical", "derived", "bool", ops=BOOL_OPS, deps=("signal_macd_golden",)),
    "kdj_k": _spec("kdj_k", "KDJ K值", "technical", "runtime", "decimal"),
    "rsi_14": _spec("rsi_14", "RSI14", "technical", "runtime", "decimal"),
    "boll_upper_breakout": _spec("boll_upper_breakout", "布林上轨突破", "technical", "derived", "bool", ops=BOOL_OPS, deps=("signal_boll_breakout_upper",)),
    "limit_up": _spec("limit_up", "涨停", "limit_up", "derived", "bool", ops=BOOL_OPS, deps=("signal_limit_up",)),
    "consecutive_limit_ups": _spec("consecutive_limit_ups", "连续涨停", "limit_up", "runtime", "decimal", unit="次"),
    "yo_y_profit": _spec("yo_y_profit", "净利润同比", "financial", "financials", "decimal", unit="%", deps=("yo_y_profit",)),
    "industry": _spec("industry", "行业", "financial", "financials", "enum", ops=ENUM_OPS, sortable=False, deps=("industry",)),
    "roe": _spec("roe", "ROE", "financial", "financials", "decimal", unit="%", deps=("weight_avg_roe",)),
    "basic_eps": _spec("basic_eps", "基本每股收益", "financial", "financials", "decimal", unit="元", deps=("basic_eps",)),
    "gross_margin": _spec("gross_margin", "毛利率", "financial", "financials", "decimal", unit="%", deps=("gross_margin",)),
    "pe_approx": _spec("pe_approx", "PE (年化近似)", "financial", "derived", "decimal", unit="倍", deps=("close", "eps_annualized")),
    "pb_approx": _spec("pb_approx", "PB (近似)", "financial", "derived", "decimal", unit="倍", deps=("close", "bps")),
    "board": _spec("board", "板块", "filter", "derived", "enum", ops=ENUM_OPS, options=_BOARD_OPTIONS, deps=("symbol",)),
    "exclude_st": _spec("exclude_st", "排除ST/退市", "filter", "derived", "bool", ops=BOOL_OPS, deps=("name",)),
    "main_net_inflow": FieldSpec("main_net_inflow", "主力净流入", "market", "unavailable", None, "numeric", "no_match", "unavailable", NUMERIC_OPS, False),
    "northbound_net_inflow": FieldSpec("northbound_net_inflow", "北向净流入", "market", "unavailable", None, "numeric", "no_match", "unavailable", NUMERIC_OPS, False),
    "realtime_concept": FieldSpec("realtime_concept", "实时概念", "filter", "unavailable", None, "enum", "no_match", "unavailable", ENUM_OPS, False),
}


_DEPRECATED = {"pb", "main_fund_flow", "ttm", "main_net_flow"}
_CURRENT_INSTRUMENT_FIELDS = {"float_market_cap", "total_market_cap", "exclude_st"}
_FINANCIAL_DERIVED_FIELDS = {"pe_approx", "pb_approx", "roe"}
_BOARD_RE = (
    (re.compile(r"^(600|601|603|605)\d{3}\.SH$"), "sh_main"),
    (re.compile(r"^(000|001|002|003)\d{3}\.SZ$"), "sz_main"),
    (re.compile(r"^(300|301)\d{3}\.SZ$"), "chinext"),
    (re.compile(r"^(688|689)\d{3}\.SH$"), "star"),
    (re.compile(r"^(?:[48]\d{5}|92\d{4})\.BJ$"), "bse"),
)


def field_metadata() -> list[dict[str, Any]]:
    return [s.metadata() for s in FIELD_REGISTRY.values()]


def _finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _validate_value(spec: FieldSpec, op: str, value: Any, location: str) -> Any:
    if spec.availability != "available":
        raise ScreenerSemanticError(location, "unavailable_field")
    if op not in spec.ops:
        raise ScreenerSemanticError(location, "unsupported_operator")
    if spec.value_type in {"numeric", "decimal", "number"}:
        vals = value if op in {"between", "in"} else [value]
        if not all(_finite_number(v) for v in vals):
            raise ScreenerSemanticError(location, "invalid_value")
        if op == "between" and value[0] > value[1]:
            raise ScreenerSemanticError(location, "invalid_value")
        return value
    if spec.value_type in {"boolean", "bool"}:
        vals = value if op == "in" else [value]
        if not all(isinstance(v, bool) for v in vals):
            raise ScreenerSemanticError(location, "invalid_value")
        return value
    if spec.value_type == "enum":
        vals = value if op == "in" else [value]
        clean: list[str] = []
        for v in vals:
            if not isinstance(v, str) or not v.strip() or len(v.strip()) > 64:
                raise ScreenerSemanticError(location, "invalid_value")
            clean.append(v.strip())
        if spec.options is not None and any(v not in spec.options for v in clean):
            raise ScreenerSemanticError(location, "invalid_value")
        return clean if op == "in" else clean[0]
    raise ScreenerSemanticError(location, "invalid_value")


def validate_query(req: ScreenerQueryRequest) -> tuple[list[dict[str, Any]], QueryOrder]:
    applied: list[dict[str, Any]] = []
    for i, cond in enumerate(req.conditions):
        location = f"conditions[{i}]"
        if cond.field not in FIELD_REGISTRY or cond.field in _DEPRECATED:
            raise ScreenerSemanticError(f"{location}.field", "unknown_field")
        spec = FIELD_REGISTRY[cond.field]
        value = _validate_value(spec, cond.op, cond.value, f"{location}.value")
        applied.append({"field": cond.field, "op": cond.op, "value": value})
    order = req.order_by or QueryOrder()
    if order.field not in FIELD_REGISTRY or order.field in _DEPRECATED:
        raise ScreenerSemanticError("order_by.field", "invalid_order_field")
    spec = FIELD_REGISTRY[order.field]
    if not spec.sortable:
        raise ScreenerSemanticError("order_by.field", "unsortable_field")
    if order.direction not in {"asc", "desc"}:
        raise ScreenerSemanticError("order_by.direction", "invalid_direction")
    return applied, order


def _board_expr() -> pl.Expr:
    symbol = pl.col("symbol").cast(pl.String, strict=False).str.to_uppercase()
    expr: pl.Expr = pl.lit(None, dtype=pl.String)
    for pattern, board in reversed(_BOARD_RE):
        expr = pl.when(symbol.str.contains(pattern.pattern)).then(pl.lit(board)).otherwise(expr)
    return expr


def _materialize(df: pl.DataFrame, required: set[str]) -> pl.DataFrame:
    expressions: list[pl.Expr] = []
    for name in required:
        if name not in FIELD_REGISTRY:
            continue
        spec = FIELD_REGISTRY[name]
        if name in df.columns:
            continue
        if name == "float_market_cap":
            expressions.append((pl.col("close") * pl.col("float_shares") / 1e8).alias(name))
        elif name == "total_market_cap":
            expressions.append((pl.col("close") * pl.col("total_shares") / 1e8).alias(name))
        elif name == "ma_bullish_alignment":
            expressions.append(
                (
                    (pl.col("ma5") > pl.col("ma10"))
                    & (pl.col("ma10") > pl.col("ma20"))
                    & (pl.col("ma20") > pl.col("ma60"))
                ).alias(name)
            )
        elif name.startswith("above_ma"):
            ma = name.removeprefix("above_")
            expressions.append((pl.col("close") > pl.col(ma)).alias(name))
        elif name.startswith("below_ma"):
            ma = name.removeprefix("below_")
            expressions.append((pl.col("close") < pl.col(ma)).alias(name))
        elif name == "macd_golden":
            expressions.append(pl.col("signal_macd_golden").alias(name))
        elif name == "boll_upper_breakout":
            expressions.append(pl.col("signal_boll_breakout_upper").alias(name))
        elif name == "limit_up":
            expressions.append(pl.col("signal_limit_up").alias(name))
        elif name == "roe":
            expressions.append(pl.col("weight_avg_roe").alias(name))
        elif name == "pe_approx":
            expressions.append(pl.when(pl.col("eps_annualized") > 0).then(pl.col("close") / pl.col("eps_annualized")).otherwise(None).alias(name))
        elif name == "pb_approx":
            expressions.append(pl.when(pl.col("bps") > 0).then(pl.col("close") / pl.col("bps")).otherwise(None).alias(name))
        elif name == "board":
            expressions.append(_board_expr().alias(name))
        elif name == "exclude_st":
            name_col = pl.col("name").cast(pl.String, strict=False).str.strip_chars()
            expressions.append(pl.when(name_col.is_null() | (name_col == "")).then(None).otherwise(~name_col.str.to_uppercase().str.contains("ST|退")).alias(name))
        elif spec.deps:
            # A source alias (e.g. financial roe) is copied only when its
            # canonical source is present; missing sources are preflighted.
            expressions.append(pl.col(spec.deps[0]).alias(name))
    if expressions:
        try:
            df = df.with_columns(expressions)
        except Exception as exc:
            raise ScreenerDataUnavailableError(sorted(required)) from exc
    return df


def _join_instruments(df: pl.DataFrame, repo: Any) -> pl.DataFrame:
    getter = getattr(repo, "get_instruments", None)
    if getter is None:
        return df
    try:
        inst = getter()
    except Exception as exc:
        raise ScreenerDataUnavailableError(["name", "total_shares", "float_shares"]) from exc
    if inst is None or inst.is_empty() or "symbol" not in inst.columns:
        return df
    cols = [c for c in ("symbol", "name", "total_shares", "float_shares") if c in inst.columns]
    inst = inst.select(cols)
    if "name" in inst.columns:
        inst = inst.with_columns(
            pl.when(pl.col("name").cast(pl.String, strict=False).str.strip_chars() == "")
            .then(None)
            .otherwise(pl.col("name"))
            .alias("name")
        )
    value_cols = [c for c in cols if c != "symbol"]
    if value_cols:
        inst = inst.group_by("symbol", maintain_order=True).agg(
            [pl.col(c).drop_nulls().last().alias(c) for c in value_cols]
        )
    # Use temporary names so existing null columns are filled independently.
    temp = {c: f"__instrument_{c}" for c in cols if c != "symbol"}
    inst = inst.rename(temp)
    try:
        if "name" in df.columns:
            df = df.with_columns(
                pl.when(pl.col("name").cast(pl.String, strict=False).str.strip_chars() == "")
                .then(None)
                .otherwise(pl.col("name"))
                .alias("name")
            )
        out = df.join(inst, on="symbol", how="left")
        for original, tmp in temp.items():
            if original in out.columns:
                out = out.with_columns(pl.coalesce([pl.col(original), pl.col(tmp)]).alias(original)).drop(tmp)
            else:
                out = out.rename({tmp: original})
        return out
    except Exception as exc:
        raise ScreenerDataUnavailableError(["name", "total_shares", "float_shares"]) from exc


def _literal_filter(condition: dict[str, Any]) -> pl.Expr:
    field, op, value = condition["field"], condition["op"], condition["value"]
    col = pl.col(field)
    if op == "between":
        expr = col.is_between(value[0], value[1], closed="both")
    elif op == "in":
        expr = col.is_in(value)
    elif op == "=":
        expr = col == pl.lit(value)
    elif op == "!=":
        expr = col != pl.lit(value)
    elif op == ">":
        expr = col > pl.lit(value)
    elif op == "<":
        expr = col < pl.lit(value)
    elif op == ">=":
        expr = col >= pl.lit(value)
    elif op == "<=":
        expr = col <= pl.lit(value)
    else:  # validate_query makes this unreachable
        raise ScreenerSemanticError("conditions", "invalid_operator")
    if FIELD_REGISTRY[field].value_type == "numeric":
        expr = col.is_not_null() & col.is_finite() & expr
    return expr.fill_null(False)


def compile_predicate(
    conditions: list[QueryCondition] | list[dict[str, Any]],
    order_by: QueryOrder | dict[str, Any] | None = None,
) -> tuple[pl.Expr, list[dict[str, Any]], QueryOrder]:
    """Validate public literals and compile a closed Polars predicate."""
    req = ScreenerQueryRequest(conditions=conditions, order_by=order_by)
    applied, order = validate_query(req)
    expression = pl.lit(True)
    for condition in applied:
        expression &= _literal_filter(condition)
    return expression, applied, order


def _json_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date,)):
        return value.isoformat()
    return value


def execute_query(repo: Any, req: ScreenerQueryRequest) -> dict[str, Any]:
    """Validate, load, materialize, filter, sort, and project a screener query."""
    mask, applied, order = compile_predicate(req.conditions, req.order_by)
    t0 = time.perf_counter()
    try:
        from app.services.screener import ScreenerService

        svc = ScreenerService(repo)
        latest = svc.latest_date()
        as_of = req.as_of or latest
        if not as_of:
            raise ScreenerDataUnavailableError(["symbol"])
        current_only = sorted(
            _CURRENT_INSTRUMENT_FIELDS
            & ({condition["field"] for condition in applied} | {order.field})
        )
        if req.as_of is not None and latest is not None and as_of != latest and current_only:
            raise ScreenerDataUnavailableError(current_only)
        df = svc._load_enriched_for_date(as_of)
    except ScreenerDataUnavailableError:
        raise
    except Exception as exc:
        raise ScreenerDataUnavailableError(["symbol"]) from exc
    if df is None or df.is_empty():
        raise ScreenerDataUnavailableError(sorted({c["field"] for c in applied}))
    if "symbol" not in df.columns:
        raise ScreenerDataUnavailableError(["symbol"])

    df = df.unique(subset=["symbol"], keep="last")
    df = _join_instruments(df, repo)
    public_required = {c["field"] for c in applied} | {order.field}
    # The baseline is always needed for the public row shape and change_pct is
    # deliberately kept as a direct enriched source.
    public_required |= {"close", "change_pct"}
    source_required: set[str] = {"symbol", "close", "change_pct"}
    for name in public_required:
        spec = FIELD_REGISTRY[name]
        if spec.source == "financials" or name in _FINANCIAL_DERIVED_FIELDS:
            source_required.update(spec.deps)
        elif name in df.columns:
            source_required.add(name)
        else:
            source_required.update(
                spec.deps
                or ((name,) if spec.source in {"persist", "persist/runtime", "runtime"} else ())
            )
    unavailable = [name for name in public_required if FIELD_REGISTRY[name].availability != "available"]
    if unavailable:
        raise ScreenerDataUnavailableError(unavailable)

    financial = {
        name for name in public_required
        if FIELD_REGISTRY[name].source == "financials"
        or name in _FINANCIAL_DERIVED_FIELDS
    }
    if financial:
        try:
            from app.services.screener_financials import load_financial_snapshot

            snap = load_financial_snapshot(Path(repo.store.data_dir), as_of)
            if snap is not None:
                if not snap.is_empty():
                    snap = snap.unique(subset=["symbol"], keep="last")
                cols = [c for c in snap.columns if c != "symbol"]
                stale = [
                    c for c in [*cols, *_FINANCIAL_DERIVED_FIELDS]
                    if c in df.columns
                ]
                if stale:
                    df = df.drop(stale)
                df = df.join(snap.select(["symbol", *cols]), on="symbol", how="left")
        except Exception as exc:
            raise ScreenerDataUnavailableError(sorted(financial)) from exc

    # Preflight canonical columns before materializing aliases.  This keeps a
    # malformed/partial enriched frame a sanitized 503, not an empty result.
    missing = [name for name in source_required if name not in df.columns]
    if missing:
        missing_public = [name for name in public_required if name in missing or any(d in missing for d in FIELD_REGISTRY[name].deps)]
        raise ScreenerDataUnavailableError(sorted(set(missing_public or missing)))
    try:
        df = _materialize(df, public_required)
        for c in public_required:
            if c not in df.columns:
                raise ScreenerDataUnavailableError([c])
        if "name" not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.String).alias("name"))
        if "date" not in df.columns:
            df = df.with_columns(pl.lit(as_of).alias("date"))

        filtered = df.filter(mask)
        total = filtered.height
        if FIELD_REGISTRY[order.field].value_type == "numeric":
            filtered = filtered.with_columns(
                pl.when(pl.col(order.field).is_finite())
                .then(pl.col(order.field))
                .otherwise(None)
                .alias(order.field)
            )
        filtered = filtered.sort(
            [order.field, "symbol"],
            descending=[order.direction == "desc", False],
            nulls_last=True,
        )
        filtered = filtered.head(req.limit)
    except ScreenerDataUnavailableError:
        raise
    except Exception as exc:
        raise ScreenerDataUnavailableError(sorted(public_required)) from exc

    # Public projection: identifiers plus requested condition/order fields. It
    # avoids pulling financial parquet for a technical-only query.
    projection: list[str] = [c for c in ("symbol", "name", "date", "close", "change_pct") if c in filtered.columns]
    for c in [*(x["field"] for x in applied), order.field]:
        if c in filtered.columns and c not in projection:
            projection.append(c)
    rows = [
        {k: _json_value(v) for k, v in row.items()}
        for row in filtered.select(projection).to_dicts()
    ]
    return {
        "rows": rows,
        "total": total,
        "applied": applied,
        "as_of": as_of.isoformat(),
        "elapsed_ms": (time.perf_counter() - t0) * 1000,
    }


class QueryService:
    """Repository-bound public entry point for condition screener queries."""

    def __init__(self, repo: Any) -> None:
        self.repo = repo

    def query(self, req: ScreenerQueryRequest) -> dict[str, Any]:
        return execute_query(self.repo, req)


__all__ = [
    "ALLOWED_OPS",
    "FIELD_REGISTRY",
    "FieldSpec",
    "QueryCondition",
    "QueryOrder",
    "QueryService",
    "ScreenerDataUnavailableError",
    "ScreenerQueryRequest",
    "ScreenerSemanticError",
    "compile_predicate",
    "execute_query",
    "field_metadata",
    "validate_query",
]
