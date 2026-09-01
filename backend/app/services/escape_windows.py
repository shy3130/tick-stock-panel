"""Pure, fail-closed calendar-window research over sealed injected readers."""

from __future__ import annotations

import contextlib
import itertools
import math
import random
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Protocol

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict

from app.data_providers.fquant.index_daily_research import IndexDailyPanel, IndexDailyReadRequest
from app.services.hold_firm_patterns.models import (
    BOOTSTRAP_ROUNDS,
    BOOTSTRAP_SEED,
    CI_LOWER_QUANTILE,
    CI_UPPER_QUANTILE,
    MIN_OOS_EVENTS,
    MIN_OOS_SYMBOLS,
)
from app.services.universe_presence_history import (
    PresenceHistoryError,
    PresenceHistoryNoCoverageError,
    PresenceHistoryNotMarketDayError,
)
from app.services.volume_breakout import (
    DEFAULT_COST_BPS,
    DEFAULT_OOS_START,
    assert_no_trading_tokens,
)

STUDY_ID = "escape_windows_v1"
STUDY_VERSION = 1
INDEX_CODES = ("000001", "000300", "000852")
ALL_A = "all_a_equal_weight"
WINDOWS = (
    "mid_december",
    "mid_april",
    "end_october",
    "end_august",
    "pre_national_day",
    "pre_spring_festival",
)
HORIZONS = (1, 5, 10, 20)
SHIFTS = tuple(range(-5, 6))
STUDY_START = date(2007, 1, 1)
STUDY_END = date(2026, 12, 31)
HOLIDAY_GAP_MIN_DAYS = 4
HOLIDAY_GAP_MAX_DAYS = 14


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EscapeWindowsRequest(_StrictModel):
    start: date = STUDY_START
    end: date = STUDY_END


class EscapeWindowsCapabilities(_StrictModel):
    canonical_panel_reader: bool = False
    versioned_exchange_calendar: bool = False
    presence_universe: bool = False
    index_daily_reader: bool = False


class EscapeWindowsResponse(_StrictModel):
    study: dict[str, Any]
    status: str
    unavailable_reasons: list[str]
    request: EscapeWindowsRequest
    capabilities: EscapeWindowsCapabilities
    parameters: dict[str, Any]
    provenance: dict[str, Any]
    coverage: dict[str, Any] | None
    legs: list[Any]
    sensitivity: list[Any]
    censored: list[Any]
    research: dict[str, Any] | None = None
    note: str


class CanonicalPanelReader(Protocol):
    def generation(self) -> str: ...
    def manifest_sha256(self) -> str: ...
    def has_columns(self, *columns: str) -> bool: ...
    def market_days(self, start: date, end: date) -> list[date]: ...
    def daily_closes(self, start: date, end: date) -> pl.DataFrame: ...


class CalendarReader(Protocol):
    def version(self) -> str: ...
    def market_days(self, start: date, end: date) -> list[date]: ...


class PresenceReader(Protocol):
    def snapshot(self, day: date) -> Any: ...


class IndexReader(Protocol):
    def read_index_daily(self, request: IndexDailyReadRequest) -> IndexDailyPanel: ...


def _study_meta() -> dict[str, Any]:
    return {
        "study_id": STUDY_ID,
        "version": STUDY_VERSION,
        "definition": "six calendar anchors; exact-day forward horizons",
        "windows": list(WINDOWS),
        "horizons": list(HORIZONS),
        "anchor_shifts": list(SHIFTS),
        "eras": ["2007-2015", "2016-2026"],
    }


def _validate_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert_no_trading_tokens(str(key))
            _validate_keys(item)
    elif isinstance(value, list):
        for item in value:
            _validate_keys(item)


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def exact_binomial_two_sided_p(positive: int, n: int) -> float | None:
    if n <= 0:
        return None
    den = float(2**n)
    lower = sum(math.comb(n, i) for i in range(positive + 1)) / den
    upper = sum(math.comb(n, i) for i in range(positive, n + 1)) / den
    return min(1.0, 2.0 * min(lower, upper))


def sign_flip_permutation_p(values: list[float]) -> float | None:
    if not values:
        return None
    arr = np.asarray(values, dtype=float)
    observed = abs(float(arr.sum()))
    if len(values) <= 20:
        sums = np.zeros(1, dtype=float)
        for value in arr:
            sums = np.concatenate((sums + value, sums - value))
        tol = 1e-10 * max(1.0, observed)
        return float(np.count_nonzero(np.abs(sums) >= observed - tol)) / float(2 ** len(values))
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    signs = rng.choice(np.array([-1.0, 1.0]), size=(10000, len(values)))
    means = np.abs(signs @ arr) / len(values)
    tol = 1e-10 * max(1.0, observed / len(values))
    return (int(np.count_nonzero(means >= observed / len(values) - tol)) + 1) / 10001.0


def _bootstrap_ci(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    if len(values) == 1:
        return {
            "lower": values[0],
            "upper": values[0],
            "rounds": BOOTSTRAP_ROUNDS,
            "seed": BOOTSTRAP_SEED,
        }
    rng = random.Random(BOOTSTRAP_SEED)
    means = [sum(rng.choices(values, k=len(values))) / len(values) for _ in range(BOOTSTRAP_ROUNDS)]
    return {
        "lower": _percentile(means, CI_LOWER_QUANTILE),
        "upper": _percentile(means, CI_UPPER_QUANTILE),
        "rounds": BOOTSTRAP_ROUNDS,
        "seed": BOOTSTRAP_SEED,
    }


def holm_adjusted(pvalues: Mapping[str, float]) -> dict[str, float]:
    ranked = sorted(pvalues.items(), key=lambda x: (x[1], x[0]))
    out: dict[str, float] = {}
    running = 0.0
    for idx, (key, p) in enumerate(ranked):
        running = max(running, min(1.0, (len(ranked) - idx) * p))
        out[key] = running
    return out


def benjamini_hochberg_adjusted(pvalues: Mapping[str, float]) -> dict[str, float]:
    ranked = sorted(pvalues.items(), key=lambda x: (x[1], x[0]))
    out: dict[str, float] = {}
    running = 1.0
    for idx in range(len(ranked) - 1, -1, -1):
        key, p = ranked[idx]
        running = min(running, min(1.0, p * len(ranked) / (idx + 1)))
        out[key] = running
    return out


def _holiday_anchor(days: list[date], year: int, window: str) -> tuple[date | None, str | None]:
    lo, hi = (
        (date(year, 9, 25), date(year, 10, 12))
        if window == "pre_national_day"
        else (date(year, 1, 10), date(year, 2, 28))
    )
    candidates = []
    for idx, (left, right) in enumerate(itertools.pairwise(days)):
        missing = (right - left).days - 1
        first = left + timedelta(days=1)
        if lo <= first <= hi and HOLIDAY_GAP_MIN_DAYS <= missing <= HOLIDAY_GAP_MAX_DAYS:
            candidates.append((missing, idx, days[idx - 4] if idx >= 4 else first))
    if not candidates:
        return None, "ANCHOR_NOT_FOUND"
    maximum = max(x[0] for x in candidates)
    best = [x for x in candidates if x[0] == maximum]
    if len(best) != 1:
        return None, "ANCHOR_AMBIGUOUS"
    return best[0][2], None


def resolve_year_anchor(days: list[date], year: int, window: str) -> tuple[date | None, str | None]:
    yd = [d for d in days if d.year == year]
    if window == "mid_december":
        value = next((d for d in yd if d.month == 12 and d.day >= 15), None)
        return value, None if value else "ANCHOR_NOT_FOUND"
    if window == "mid_april":
        value = next((d for d in yd if d.month == 4 and d.day >= 15), None)
        return value, None if value else "ANCHOR_NOT_FOUND"
    if window == "end_october":
        vals = [d for d in yd if d.month == 10]
        return (max(vals), None) if vals else (None, "ANCHOR_NOT_FOUND")
    if window == "end_august":
        vals = [d for d in yd if d.month == 8]
        return (max(vals), None) if vals else (None, "ANCHOR_NOT_FOUND")
    return _holiday_anchor(days, year, window)


def _equal_weight_returns(
    panel: pl.DataFrame, days: list[date]
) -> tuple[dict[date, float], dict[date, frozenset[str]], dict[str, int]]:
    audit = {
        "rows_total": panel.height,
        "rows_removed_nonpositive_volume": 0,
        "rows_removed_nonpositive_close": 0,
        "rows_outside_market_days": 0,
        "return_contributor_rows": 0,
    }
    if panel.is_empty():
        return {}, {}, audit
    before = panel.height
    frame = panel.filter(pl.col("volume").cast(pl.Float64, strict=False) > 0)
    audit["rows_removed_nonpositive_volume"] = before - frame.height
    before = frame.height
    frame = frame.filter(pl.col("close").cast(pl.Float64, strict=False) > 0)
    audit["rows_removed_nonpositive_close"] = before - frame.height
    cal = pl.DataFrame({"date": days, "day_index": list(range(len(days)))})
    before = frame.height
    frame = frame.join(cal, on="date", how="inner").sort(["symbol", "day_index"])
    audit["rows_outside_market_days"] = before - frame.height
    frame = frame.with_columns(
        [
            pl.col("close").cast(pl.Float64),
            pl.col("day_index").shift(1).over("symbol").alias("prev_index"),
            pl.col("close").shift(1).over("symbol").alias("prev_close"),
        ]
    )
    valid = frame.filter(pl.col("day_index") == pl.col("prev_index") + 1).with_columns(
        (pl.col("close") / pl.col("prev_close") - 1).alias("daily_return")
    )
    audit["return_contributor_rows"] = valid.height
    grouped = valid.group_by("date").agg(
        [pl.col("daily_return").mean().alias("ret"), pl.col("symbol").alias("symbols")]
    )
    return (
        {r["date"]: float(r["ret"]) for r in grouped.to_dicts()},
        {r["date"]: frozenset(r["symbols"]) for r in grouped.to_dicts()},
        audit,
    )


def _leg_returns(
    bars: list[dict[str, Any]],
    positions: Mapping[date, int],
) -> tuple[dict[date, float], set[date]]:
    ordered = sorted(bars, key=lambda x: x["date"])
    values = {}
    for left, right in itertools.pairwise(ordered):
        left_day, right_day = left["date"], right["date"]
        if positions.get(right_day) != positions.get(left_day, -2) + 1:
            continue
        with contextlib.suppress(TypeError, ValueError, ZeroDivisionError):
            values[right_day] = float(right["close"]) / float(left["close"]) - 1.0
    return values, {x["date"] for x in ordered}


def _complete_forward_return(
    returns: Mapping[date, float],
    market_days: Sequence[date],
    start_pos: int,
    horizon: int,
) -> tuple[float | None, str | None]:
    end_pos = start_pos + horizon
    if start_pos < 0 or end_pos >= len(market_days):
        return None, "HORIZON_TRUNCATED"
    span = market_days[start_pos + 1 : end_pos + 1]
    if len(span) != horizon or any(day not in returns for day in span):
        return None, "LEG_SPAN_INCOMPLETE"
    value = math.prod(1.0 + float(returns[day]) for day in span) - 1.0
    return value, None


def _substats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [float(x["return"]) for x in rows]
    p = sum(x > 0 for x in vals)
    return {
        "n_years": len(vals),
        "positive_rate": p / len(vals) if vals else None,
        "mean_return": sum(vals) / len(vals) if vals else None,
    }


def _cell(leg: str, window: str, horizon: int, years: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [float(x["return"]) for x in years]
    n, p = len(vals), sum(x > 0 for x in vals)
    oos = [x for x in years if x["in_oos"]]
    return {
        "leg": leg,
        "window": window,
        "horizon_days": horizon,
        "status": "ok" if n else "unavailable",
        "n_years": n,
        "positive_years": p,
        "positive_rate": p / n if n else None,
        "mean_return": sum(vals) / n if n else None,
        "binomial_p_exact": exact_binomial_two_sided_p(p, n),
        "permutation_p": sign_flip_permutation_p(vals),
        "mean_ci95": _bootstrap_ci(vals),
        "years": years,
        "eras": {
            "2007-2015": _substats([x for x in years if x["year"] <= 2015]),
            "2016-2026": _substats([x for x in years if x["year"] >= 2016]),
        },
        "oos": _substats(oos) | {"claimable": len(oos) >= MIN_OOS_EVENTS},
    }


def _presence_audit(
    presence: PresenceReader | None, symbols: Mapping[date, frozenset[str]]
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "unavailable" if presence is None else "ok",
        "days_compared": 0,
        "days_identical": 0,
        "canonical_only_symbols": 0,
        "presence_only_symbols": 0,
        "mismatch_days": [],
    }
    if presence is None:
        out["reason"] = "presence_universe_missing"
        return out
    for day in sorted(symbols):
        try:
            ps = frozenset(presence.snapshot(day).symbols)
        except (PresenceHistoryNoCoverageError, PresenceHistoryNotMarketDayError):
            continue
        except PresenceHistoryError as exc:
            out.update(status="unavailable", reason="presence_crosscheck_error", detail=str(exc))
            break
        cs = symbols[day]
        extra, missing = cs - ps, ps - cs
        out["days_compared"] += 1
        out["canonical_only_symbols"] += len(extra)
        out["presence_only_symbols"] += len(missing)
        if not extra and not missing:
            out["days_identical"] += 1
        elif len(out["mismatch_days"]) < 10:
            out["mismatch_days"].append(
                {"date": day, "canonical_only": len(extra), "presence_only": len(missing)}
            )
    return out


def assess_escape_windows_capability(repo: Any) -> dict[str, Any]:
    def capable(name: str, required: tuple[str, ...]) -> Any | None:
        obj = getattr(repo, name, None)
        return obj if all(callable(getattr(obj, attr, None)) for attr in required) else None

    canonical = capable(
        "generation_pinned_daily_reader",
        ("generation", "manifest_sha256", "has_columns", "market_days", "daily_closes"),
    )
    calendar = capable("versioned_exchange_calendar", ("version", "market_days"))
    presence = capable("pit_presence_universe", ("snapshot",))
    index = getattr(repo, "index_daily_research_reader", None)
    try:
        capabilities = EscapeWindowsCapabilities(
            canonical_panel_reader=canonical is not None,
            versioned_exchange_calendar=calendar is not None,
            presence_universe=presence is not None,
            index_daily_reader=index is not None,
        )
        return {
            "status": ("ok" if canonical is not None and calendar is not None else "unavailable"),
            "capabilities": capabilities.model_dump(),
            "missing": [
                name
                for name, reader in (
                    ("canonical_panel_reader", canonical),
                    ("versioned_exchange_calendar", calendar),
                )
                if reader is None
            ],
        }
    finally:
        close = getattr(index, "close", None)
        if callable(close):
            close()


def evaluate_escape_windows(
    request: EscapeWindowsRequest | Mapping[str, Any],
    *,
    canonical_reader: CanonicalPanelReader | None,
    calendar: CalendarReader | None,
    presence_universe: PresenceReader | None = None,
    index_reader: IndexReader | None = None,
) -> dict[str, Any]:
    req = (
        request
        if isinstance(request, EscapeWindowsRequest)
        else EscapeWindowsRequest.model_validate(request)
    )
    if req.start > req.end:
        raise ValueError("start must be <= end")
    caps = EscapeWindowsCapabilities(
        canonical_panel_reader=canonical_reader is not None,
        versioned_exchange_calendar=calendar is not None,
        presence_universe=presence_universe is not None,
        index_daily_reader=index_reader is not None,
    )
    missing = []
    if canonical_reader is None:
        missing.append("canonical_panel_reader_missing")
    if calendar is None:
        missing.append("versioned_exchange_calendar_missing")
    if canonical_reader is not None and not canonical_reader.has_columns("close", "volume"):
        missing.append("canonical_close_volume_columns_missing")
    if missing:
        payload = {
            "study": _study_meta(),
            "status": "unavailable",
            "unavailable_reasons": missing,
            "request": req.model_dump(),
            "capabilities": caps.model_dump(),
            "parameters": {"bootstrap_seed": BOOTSTRAP_SEED},
            "provenance": {},
            "coverage": None,
            "legs": [],
            "sensitivity": [],
            "censored": [],
            "research": None,
            "note": "缺少 sealed 能力时不产生研究结果",
        }
        _validate_keys(payload)
        return EscapeWindowsResponse.model_validate(payload).model_dump(mode="json")
    assert canonical_reader is not None and calendar is not None
    days = sorted(set(calendar.market_days(req.start - timedelta(days=30), req.end)))
    positions = {d: i for i, d in enumerate(days)}
    if not days:
        return evaluate_escape_windows(
            req, canonical_reader=None, calendar=None, presence_universe=None, index_reader=None
        )
    panel = canonical_reader.daily_closes(req.start - timedelta(days=30), req.end)
    eq_returns, eq_symbols, audit = _equal_weight_returns(panel, days)
    series: dict[str, tuple[dict[date, float], set[date], dict[str, Any]]] = {
        ALL_A: (eq_returns, set(eq_returns), audit)
    }
    pin: dict[str, Any] = {}
    if index_reader is not None:
        index_panel = index_reader.read_index_daily(
            IndexDailyReadRequest(
                codes=list(INDEX_CODES), start=req.start - timedelta(days=30), end=req.end
            )
        )
        pin = index_panel.pin.model_dump(mode="json")
        for leg in index_panel.legs:
            if leg.status == "ok":
                series[leg.code] = (
                    *_leg_returns([x.model_dump() for x in leg.bars], positions),
                    leg.coverage.model_dump(mode="json"),
                )
            else:
                series[leg.code] = (
                    {},
                    set(),
                    {
                        "status": "unavailable",
                        "reason_code": leg.reason_code,
                        "coverage": leg.coverage.model_dump(mode="json"),
                    },
                )
    legs, censored, sensitivity = [], [], []
    for leg in (*INDEX_CODES, ALL_A):
        returns, leg_days, leg_cov = series.get(
            leg, ({}, set(), {"status": "unavailable", "reason_code": "INDEX_SOURCE_UNAVAILABLE"})
        )
        cells = []
        if not returns and leg != ALL_A:
            legs.append(
                {
                    "leg": leg,
                    "status": "unavailable",
                    "reason_code": leg_cov.get("reason_code", "INDEX_SOURCE_UNAVAILABLE"),
                    "cells": [],
                    "coverage": leg_cov,
                }
            )
            continue
        for window in WINDOWS:
            for horizon in HORIZONS:
                rows = []
                for year in range(max(2007, req.start.year), min(2026, req.end.year) + 1):
                    anchor, reason = resolve_year_anchor(days, year, window)
                    if anchor is None or reason:
                        censored.append(
                            {
                                "leg": leg,
                                "window": window,
                                "year": year,
                                "horizon_days": horizon,
                                "reason": reason or "ANCHOR_NOT_FOUND",
                            }
                        )
                        continue
                    pos = positions.get(anchor)
                    if pos is None or anchor not in leg_days:
                        censored.append(
                            {
                                "leg": leg,
                                "window": window,
                                "year": year,
                                "horizon_days": horizon,
                                "reason": "MISSING_LEG_BAR",
                            }
                        )
                        continue
                    forward_return, span_reason = _complete_forward_return(
                        returns,
                        days,
                        pos,
                        horizon,
                    )
                    if span_reason is not None or forward_return is None:
                        censored.append(
                            {
                                "leg": leg,
                                "window": window,
                                "year": year,
                                "horizon_days": horizon,
                                "reason": span_reason or "LEG_SPAN_INCOMPLETE",
                            }
                        )
                        continue
                    rows.append(
                        {
                            "year": year,
                            "anchor_date": anchor,
                            "return": forward_return,
                            "positive": forward_return > 0.0,
                            "in_oos": anchor >= DEFAULT_OOS_START,
                        }
                    )
                cells.append(_cell(leg, window, horizon, rows))
                for shift in SHIFTS:
                    shifted = []
                    for row in rows:
                        p0 = positions.get(row["anchor_date"], -1) + shift
                        if not (0 <= p0 < len(days)) or days[p0] not in leg_days:
                            censored.append(
                                {
                                    "leg": leg,
                                    "window": window,
                                    "year": row["year"],
                                    "horizon_days": horizon,
                                    "shift": shift,
                                    "reason": "SENSITIVITY_ANCHOR_UNAVAILABLE",
                                }
                            )
                            continue
                        shifted_return, shifted_reason = _complete_forward_return(
                            returns,
                            days,
                            p0,
                            horizon,
                        )
                        if shifted_reason is None and shifted_return is not None:
                            shifted.append(shifted_return)
                        else:
                            censored.append(
                                {
                                    "leg": leg,
                                    "window": window,
                                    "year": row["year"],
                                    "horizon_days": horizon,
                                    "shift": shift,
                                    "reason": shifted_reason or "SENSITIVITY_ANCHOR_UNAVAILABLE",
                                }
                            )
                    sensitivity.append(
                        {
                            "leg": leg,
                            "window": window,
                            "horizon_days": horizon,
                            "shift": shift,
                            "n_years": len(shifted),
                            "positive_rate": sum(x > 0 for x in shifted) / len(shifted)
                            if shifted
                            else None,
                            "mean_return": sum(shifted) / len(shifted) if shifted else None,
                            "descriptive": True,
                        }
                    )
        legs.append({"leg": leg, "status": "ok", "cells": cells, "coverage": leg_cov})
    pbin = {
        f"{c['leg']}|{c['window']}|{c['horizon_days']}": c["binomial_p_exact"]
        for leg_result in legs
        for c in leg_result.get("cells", [])
        if c.get("binomial_p_exact") is not None
    }
    pperm = {
        f"{c['leg']}|{c['window']}|{c['horizon_days']}": c["permutation_p"]
        for leg_result in legs
        for c in leg_result.get("cells", [])
        if c.get("permutation_p") is not None
    }
    hbin, bbin, hperm, bperm = (
        holm_adjusted(pbin),
        benjamini_hochberg_adjusted(pbin),
        holm_adjusted(pperm),
        benjamini_hochberg_adjusted(pperm),
    )
    for leg_result in legs:
        for c in leg_result.get("cells", []):
            key = f"{c['leg']}|{c['window']}|{c['horizon_days']}"
            c.update(
                {
                    "holm_binomial_p": hbin.get(key),
                    "bh_binomial_p": bbin.get(key),
                    "holm_permutation_p": hperm.get(key),
                    "bh_permutation_p": bperm.get(key),
                }
            )
    provenance = {
        "canonical_generation": canonical_reader.generation(),
        "canonical_manifest_sha256": canonical_reader.manifest_sha256(),
        "calendar_version": calendar.version(),
        "index_pin": pin,
        "equal_weight_rule": "exact-day rows with positive volume and immediately previous market-day row",
        "index_merge_rule": "klines base plus markets rows strictly after per-code klines maximum",
        "presence_crosscheck": _presence_audit(presence_universe, eq_symbols),
        "seeds": {"bootstrap": BOOTSTRAP_SEED, "permutation": BOOTSTRAP_SEED},
        "frozen_constants": {
            "oos_start": DEFAULT_OOS_START,
            "cost_bps": DEFAULT_COST_BPS,
            "min_oos_events": MIN_OOS_EVENTS,
            "min_oos_symbols": MIN_OOS_SYMBOLS,
        },
    }
    payload = {
        "study": _study_meta(),
        "status": "ok",
        "unavailable_reasons": [],
        "request": req.model_dump(),
        "capabilities": caps.model_dump(),
        "parameters": {
            "holiday_gap_min_days": HOLIDAY_GAP_MIN_DAYS,
            "holiday_gap_max_days": HOLIDAY_GAP_MAX_DAYS,
            "bootstrap_rounds": BOOTSTRAP_ROUNDS,
            "sensitivity_descriptive": True,
        },
        "provenance": provenance,
        "coverage": {
            "calendar_days": len(days),
            "study_years": list(range(max(2007, req.start.year), min(2026, req.end.year) + 1)),
            "all_a": audit,
        },
        "legs": legs,
        "sensitivity": sensitivity,
        "censored": censored,
        "research": {
            "verdict": "no_effect_concluded",
            "basis": "descriptive replication pending independent comparison",
        },
        "note": "仅用于可复现的历史研究诊断, 不构成交易语义",
    }
    _validate_keys(payload)
    return EscapeWindowsResponse.model_validate(payload).model_dump(mode="json")


__all__ = [
    "ALL_A",
    "HORIZONS",
    "INDEX_CODES",
    "SHIFTS",
    "WINDOWS",
    "EscapeWindowsRequest",
    "EscapeWindowsResponse",
    "assess_escape_windows_capability",
    "benjamini_hochberg_adjusted",
    "evaluate_escape_windows",
    "exact_binomial_two_sided_p",
    "holm_adjusted",
    "resolve_year_anchor",
    "sign_flip_permutation_p",
]
