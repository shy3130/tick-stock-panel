"""Auditable single-yang-no-break research over one immutable raw generation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any, Protocol

import polars as pl

from app.services.hold_firm_patterns.models import (
    MIN_VALID_BOOTSTRAP_REPLICATES,
    PRICE_ABS_TOL,
)
from app.services.hold_firm_patterns.statistics import gates, selection_cluster_bootstrap

RESEARCH_ID = "single_yang_no_break_v1"
DEFAULT_WINDOW = 5
MIN_BODY_PCT_OF_OPEN = 0.02
_REQUIRED_READER_METHODS = ("generation", "manifest_sha256", "market_days", "daily_bars", "columns")
_REQUIRED_RAW_COLUMNS = ("raw_open", "raw_high", "raw_low", "raw_close")
_LIMIT_FACT_COLUMN = "limit_up_price"

SINGLE_YANG_DEFINITION: dict[str, Any] = {
    "id": RESEARCH_ID,
    "price_basis": "raw_unadjusted",
    "yang": "close > open",
    "body": "close - open",
    "upper_shadow": "high - max(open, close)",
    "lower_shadow": "min(open, close) - low",
    "min_body_pct_of_open": MIN_BODY_PCT_OF_OPEN,
    "anchor": "low",
    "break_rule": "subsequent low < anchor low",
    "equal_low": "touch_is_not_break",
    "window": DEFAULT_WINDOW,
    "window_unit": "trading_days_after_T",
    "signal_timing": "T_plus_5_close_confirmed; evaluation_starts_T_plus_6",
    "oos": "required",
}


class GenerationPinnedRawReader(Protocol):
    def generation(self) -> str: ...
    def manifest_sha256(self) -> str: ...
    def columns(self) -> Sequence[str]: ...
    def market_days(self, start: date, end: date) -> list[date]: ...
    def daily_bars(self, symbol: str, start: date, end: date): ...


class SingleYangCompositeReader:
    """Bind canonical bars to the exact markets generation pinned by that manifest."""

    def __init__(self, canonical: GenerationPinnedRawReader, market_facts: Any) -> None:
        self._canonical = canonical
        self._market_facts = market_facts

    def generation(self) -> str:
        return self._canonical.generation()

    def manifest_sha256(self) -> str:
        return self._canonical.manifest_sha256()

    def columns(self) -> Sequence[str]:
        return (*self._canonical.columns(), _LIMIT_FACT_COLUMN)

    def market_days(self, start: date, end: date) -> list[date]:
        return self._canonical.market_days(start, end)

    def daily_bars(self, symbol: str, start: date, end: date):
        frame = self._canonical.daily_bars(symbol, start, end)
        if frame is None or frame.is_empty():
            return frame
        facts = self._market_facts.limit_band_facts(symbol, start, end)
        values = [
            getattr(facts.get(day), "published_limit_up", None)
            for day in frame.get_column("date").to_list()
        ]
        return frame.with_columns(pl.Series(_LIMIT_FACT_COLUMN, values, dtype=pl.Float64))

    def source_provenance(self) -> dict[str, Any]:
        return {
            "canonical": {
                "generation": self._canonical.generation(),
                "manifest_sha256": self._canonical.manifest_sha256(),
            },
            "market_facts": {
                "generation": self._market_facts.generation(),
                "manifest_sha256": self._market_facts.manifest_sha256(),
            },
        }


@dataclass(frozen=True)
class Bar:
    """One raw, unadjusted daily bar from a single generation."""

    open: float
    high: float
    low: float
    close: float


def is_single_yang(bar: Bar) -> bool:
    if not (bar.close > bar.open and bar.open > 0):
        return False
    open_price = Decimal(str(bar.open))
    close_price = Decimal(str(bar.close))
    return (close_price - open_price) / open_price >= Decimal(str(MIN_BODY_PCT_OF_OPEN))


def detect_single_yang(bars: Sequence[Bar]) -> list[int]:
    """Return anchors only after the complete five-market-day confirmation window."""
    signals: list[int] = []
    last_anchor = len(bars) - DEFAULT_WINDOW - 1
    for index, bar in enumerate(bars[: max(0, last_anchor + 1)]):
        if not is_single_yang(bar):
            continue
        follow_up = bars[index + 1 : index + DEFAULT_WINDOW + 1]
        if all(next_bar.low >= bar.low for next_bar in follow_up):
            signals.append(index)
    return signals


def assess_capability(reader: Any | None = None) -> dict[str, Any]:
    if reader is None:
        return {"available": False, "reasons": ["generation_pinned_reader_missing"]}
    if not all(callable(getattr(reader, name, None)) for name in _REQUIRED_READER_METHODS):
        return {"available": False, "reasons": ["generation_pinned_reader_invalid"]}
    missing = [column for column in _REQUIRED_RAW_COLUMNS if column not in set(reader.columns())]
    if missing:
        return {
            "available": False,
            "reasons": ["raw_generation_columns_missing"],
            "missing_columns": missing,
        }
    return {"available": True, "reasons": []}


def _unavailable(reasons: list[str], *, missing_columns: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reasons": reasons,
        "missing_columns": missing_columns or [],
        "definition": dict(SINGLE_YANG_DEFINITION),
        "provenance": {},
        "events": [],
        "censored": [],
        "segments": {"is": {"events": 0}, "oos": {"events": 0}},
    }


def evaluate_single_yang(
    *,
    reader: GenerationPinnedRawReader | None,
    start: date,
    end: date,
    symbols: list[str],
    oos_start: date,
    cost_bps: float = 10.0,
) -> dict[str, Any]:
    if start > end:
        raise ValueError("start must be <= end")
    if not symbols:
        raise ValueError("symbols must not be empty")
    if not start <= oos_start <= end:
        raise ValueError("oos_start must be within [start, end]")
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("cost_bps must be finite and >= 0")

    capability = assess_capability(reader)
    if not capability["available"]:
        return _unavailable(
            capability["reasons"],
            missing_columns=capability.get("missing_columns"),
        )
    assert reader is not None
    generation = reader.generation()
    manifest_hash = reader.manifest_sha256()
    if not isinstance(manifest_hash, str) or len(manifest_hash) != 64:
        return _unavailable(["reader_manifest_identity_invalid"])

    lookup_start = start - timedelta(days=30)
    calendar = sorted(set(reader.market_days(lookup_start, end + timedelta(days=20))))
    calendar_pos = {day: index for index, day in enumerate(calendar)}
    events: list[dict[str, Any]] = []
    censored: list[dict[str, Any]] = []

    for symbol in sorted(set(symbols)):
        frame = reader.daily_bars(symbol, lookup_start, end + timedelta(days=20))
        if frame is None or frame.is_empty():
            censored.append({"symbol": symbol, "code": "no_data"})
            continue
        missing = [
            column for column in ("date", *_REQUIRED_RAW_COLUMNS) if column not in frame.columns
        ]
        if missing:
            censored.append({"symbol": symbol, "code": "raw_field_missing", "fields": missing})
            continue
        rows: list[dict[str, Any]] = []
        bad = False
        for row in frame.sort("date").to_dicts():
            values = [row.get(column) for column in _REQUIRED_RAW_COLUMNS]
            if any(
                not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
                for value in values
            ):
                censored.append(
                    {"symbol": symbol, "code": "raw_field_invalid", "date": str(row.get("date"))}
                )
                bad = True
                break
            row_generation = row.get("generation")
            if row_generation is not None and row_generation != generation:
                censored.append(
                    {"symbol": symbol, "code": "generation_mismatch", "date": str(row.get("date"))}
                )
                bad = True
                break
            rows.append(row)
        if bad:
            continue
        bars = [
            Bar(
                float(row["raw_open"]),
                float(row["raw_high"]),
                float(row["raw_low"]),
                float(row["raw_close"]),
            )
            for row in rows
        ]
        for anchor_index in detect_single_yang(bars):
            anchor_day = rows[anchor_index]["date"]
            anchor_position = calendar_pos.get(anchor_day)
            if anchor_position is None:
                censored.append(
                    {
                        "symbol": symbol,
                        "code": "anchor_calendar_date_missing",
                        "anchor_date": anchor_day,
                    }
                )
                continue
            expected_follow_days = calendar[
                anchor_position + 1 : anchor_position + DEFAULT_WINDOW + 1
            ]
            if len(expected_follow_days) != DEFAULT_WINDOW:
                censored.append(
                    {
                        "symbol": symbol,
                        "code": "confirmation_window_truncated",
                        "anchor_date": anchor_day,
                    }
                )
                continue
            confirm_index = anchor_index + DEFAULT_WINDOW
            actual_follow_days = [row["date"] for row in rows[anchor_index + 1 : confirm_index + 1]]
            if actual_follow_days != expected_follow_days:
                censored.append(
                    {
                        "symbol": symbol,
                        "code": "confirmation_window_incomplete",
                        "anchor_date": anchor_day,
                        "missing_market_days": [
                            day for day in expected_follow_days if day not in actual_follow_days
                        ],
                    }
                )
                continue
            confirm_day = expected_follow_days[-1]
            if anchor_day < start or confirm_day > end:
                continue
            position = calendar_pos.get(confirm_day)
            if position is None or position + 1 >= len(calendar):
                censored.append(
                    {
                        "symbol": symbol,
                        "code": "next_market_day_unavailable",
                        "anchor_date": anchor_day,
                    }
                )
                continue
            available_from = calendar[position + 1]
            next_row = next((row for row in rows if row["date"] == available_from), None)
            if next_row is None:
                censored.append(
                    {"symbol": symbol, "code": "forward_bar_missing", "anchor_date": anchor_day}
                )
                continue
            anchor = bars[anchor_index]
            follow = bars[anchor_index + 1 : confirm_index + 1]
            gross = float(next_row["raw_close"]) / bars[confirm_index].close - 1.0
            body_ratio = (Decimal(str(anchor.close)) - Decimal(str(anchor.open))) / Decimal(
                str(anchor.open)
            )
            events.append(
                {
                    "symbol": symbol,
                    "anchor_date": anchor_day,
                    "confirm_date": confirm_day,
                    "available_from": available_from,
                    "segment": "oos" if available_from >= oos_start else "is",
                    "evidence": {
                        "raw_open": anchor.open,
                        "raw_high": anchor.high,
                        "raw_low": anchor.low,
                        "raw_close": anchor.close,
                        "body_ratio": float(body_ratio),
                        "follow_lows": [bar.low for bar in follow],
                        "equal_low_touches": sum(bar.low == anchor.low for bar in follow),
                    },
                    "forward": {
                        "horizon": "confirmation_to_next_market_day",
                        "gross_return": gross,
                        "cost_bps": cost_bps,
                        "post_cost_return": gross - cost_bps / 10000.0,
                        "reachability": "daily_price_only",
                    },
                }
            )

    segments = {
        name: {"events": sum(event["segment"] == name for event in events)}
        for name in ("is", "oos")
    }
    return {
        "status": "ok",
        "reasons": [],
        "definition": dict(SINGLE_YANG_DEFINITION),
        "provenance": {
            "generation": generation,
            "manifest_sha256": manifest_hash.lower(),
            "raw_columns": list(_REQUIRED_RAW_COLUMNS),
            "oos_start": oos_start,
            "cost_bps": cost_bps,
        },
        "events": events,
        "censored": censored,
        "segments": segments,
    }


def run_single_yang_research(*, bars: Sequence[Bar] | None = None) -> dict[str, Any]:
    """Backwards-compatible capability endpoint; explicit bars never bypass sealing."""
    del bars
    return _unavailable(["generation_pinned_reader_missing"])


DEFAULT_HOLD_HORIZONS = (1, 2, 3, 5, 10, 20, 60)
PRIMARY_HORIZONS = (5, 10, 20)
INCREMENT_RESEARCH_ID = "single_yang_no_break_increment_v1"
_OVERLAP_GAP = max(PRIMARY_HORIZONS)

INCREMENT_DEFINITION: dict[str, Any] = {
    "id": INCREMENT_RESEARCH_ID,
    "pattern_arm": "single_yang_no_break_v1 anchors (body >= 2% of open, 5-day no-break of anchor low)",
    "baseline_arm": "all tradable first-board limit-ups under exact-date limit_up_price PIT facts",
    "entry_rule": (
        "close-confirmed signals fill at the raw_open of the next market day (available_from / entry_date); "
        "horizon h exits at the close of entry_date + h trading days, so every hold respects T+1"
    ),
    "reachability_rule": (
        "entry-day raw_open >= limit_up_price - 0.005 cannot be proven fillable at the open and the event is "
        "excluded from achievable statistics (fail-closed); missing limit facts leave reachability unknown"
    ),
    "limit_fact_rule": (
        "the baseline arm requires an exact-date limit_up_price fact per row; prefix-based 10/20/30% approximations "
        "are prohibited because historical ST and special regimes are not identifiable from the symbol alone; "
        "missing facts force the baseline arm and the overall verdict unavailable"
    ),
    "return_price_basis": "canonical adjusted open/close columns; raw_open remains only for reachability and raw OHLC evidence",
    "volume_subtyping": "descriptive raw_volume contraction ratio over the confirmation window vs the anchor day; never a filter; unavailable when raw_volume facts are missing",
    "hold_horizons_trading_days": list(DEFAULT_HOLD_HORIZONS),
    "primary_horizons": list(PRIMARY_HORIZONS),
    "cost_model": "post_cost = gross - cost_bps / 10000 (one round trip)",
    "horizon_censoring": "a horizon is complete only when the symbol has a raw bar exactly h trading days after entry; missing bars or data end censor that horizon, never carried forward",
    "overlap_policy": "entries within 20 trading days of a prior same-symbol entry are disclosed as overlapping and handled by symbol-cluster bootstrap; no events are dropped",
    "oos_nature": "expanded_generation_pinned_sample_not_pristine",
}


def _fact_sealed(price: float, limit_up_price: Any) -> bool | None:
    if (
        not isinstance(limit_up_price, (int, float))
        or not math.isfinite(float(limit_up_price))
        or float(limit_up_price) <= 0
    ):
        return None
    return price >= float(limit_up_price) - PRICE_ABS_TOL


def _wilson(wins: int, n: int) -> list[float] | None:
    if not n:
        return None
    z = 1.959963984540054
    p = wins / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [max(0.0, c - h), min(1.0, c + h)]


def _simple_stats(values: Sequence[float], symbols: set[str]) -> dict[str, Any]:
    values = list(values)
    if not values:
        return {"n": 0, "symbols": 0}
    wins = sum(value > 0 for value in values)
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    ordered = sorted(values)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "n": len(values),
        "symbols": len(symbols),
        "mean_post_cost_return": sum(values) / len(values),
        "median_post_cost_return": median,
        "win_rate": wins / len(values),
        "win_rate_ci95": _wilson(wins, len(values)),
        "profit_factor": gains / losses if losses else None,
        "worst_trade_post_cost_return": min(values),
    }


def _validated_rows(
    reader: Any, symbol: str, generation: str, start: date, end: date
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    frame = reader.daily_bars(symbol, start, end)
    if frame is None or frame.is_empty():
        return None, {"symbol": symbol, "code": "no_data"}
    missing = [
        column
        for column in ("date", *_REQUIRED_RAW_COLUMNS, "open", "close")
        if column not in frame.columns
    ]
    if missing:
        return None, {"symbol": symbol, "code": "raw_field_missing", "fields": missing}
    rows: list[dict[str, Any]] = []
    for row in frame.sort("date").to_dicts():
        values = [row.get(column) for column in _REQUIRED_RAW_COLUMNS]
        if any(
            not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in values
        ):
            return None, {
                "symbol": symbol,
                "code": "raw_field_invalid",
                "date": str(row.get("date")),
            }
        if any(
            not isinstance(row.get(column), (int, float))
            or not math.isfinite(float(row[column]))
            or float(row[column]) <= 0
            for column in ("open", "close")
        ):
            return None, {
                "symbol": symbol,
                "code": "adjusted_field_invalid",
                "date": str(row.get("date")),
            }
        if row.get("generation") is not None and row["generation"] != generation:
            return None, {
                "symbol": symbol,
                "code": "generation_mismatch",
                "date": str(row.get("date")),
            }
        rows.append(row)
    return rows, None


def evaluate_single_yang_increment(
    *,
    reader: GenerationPinnedRawReader | None,
    start: date,
    end: date,
    symbols: list[str],
    oos_start: date,
    cost_bps: float = 10.0,
    hold_horizons: Sequence[int] = DEFAULT_HOLD_HORIZONS,
) -> dict[str, Any]:
    if start > end:
        raise ValueError("start must be <= end")
    if not symbols:
        raise ValueError("symbols must not be empty")
    if not start <= oos_start <= end:
        raise ValueError("oos_start must be within [start, end]")
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("cost_bps must be finite and >= 0")
    horizons = tuple(sorted(set(hold_horizons)))
    if not horizons or any(not isinstance(h, int) or h <= 0 for h in horizons):
        raise ValueError("hold_horizons must contain positive integers")
    capability = assess_capability(reader)
    if not capability["available"]:
        result = _unavailable(
            capability["reasons"], missing_columns=capability.get("missing_columns")
        )
        result["research_id"] = INCREMENT_RESEARCH_ID
        return result
    assert reader is not None
    generation = reader.generation()
    manifest_hash = reader.manifest_sha256()
    if not isinstance(manifest_hash, str) or len(manifest_hash) != 64:
        result = _unavailable(["reader_manifest_identity_invalid"])
        result["research_id"] = INCREMENT_RESEARCH_ID
        return result
    calendar = sorted(
        set(reader.market_days(start - timedelta(days=30), end + timedelta(days=140)))
    )
    calendar_pos = {day: index for index, day in enumerate(calendar)}
    columns = set(reader.columns())
    limit_fact_present = _LIMIT_FACT_COLUMN in columns
    volume_available = "raw_volume" in columns
    baseline_fact_incomplete = not limit_fact_present
    pattern_events: list[dict[str, Any]] = []
    missing_adjusted = [column for column in ("open", "close") if column not in columns]
    if missing_adjusted:
        result = _unavailable(["adjusted_price_columns_missing"], missing_columns=missing_adjusted)
        result["research_id"] = INCREMENT_RESEARCH_ID
        return result
    baseline_events: list[dict[str, Any]] = []
    censored: list[dict[str, Any]] = []
    coverage = {
        "pattern": {
            "yang_anchors_scanned": 0,
            "structure_break": 0,
            "events_detected": 0,
            "entry_unreachable_sealed": 0,
            "entry_reachability_unknown": 0,
            "achievable_events": 0,
            "volume_subtyping": {
                "available": volume_available,
                **({} if volume_available else {"reason": "volume_column_missing"}),
            },
        },
        "baseline": {
            "first_boards_detected": 0,
            "entry_unreachable_sealed": 0,
            "achievable_events": 0,
        },
        "horizons": {
            arm: {
                segment: {str(h): {"complete": 0, "censored": 0} for h in horizons}
                for segment in ("is", "oos")
            }
            for arm in ("pattern", "baseline")
        },
        "overlap": {
            "pattern": {"events": 0, "overlapping_events": 0},
            "baseline": {"events": 0, "overlapping_events": 0},
        },
    }
    returns: dict[str, dict[str, dict[int, dict[str, list[float]]]]] = {
        arm: {segment: {h: {} for h in horizons} for segment in ("is", "oos")}
        for arm in ("pattern", "baseline")
    }
    for symbol in sorted(set(symbols)):
        rows, failure = _validated_rows(
            reader, symbol, generation, start - timedelta(days=30), end + timedelta(days=140)
        )
        if failure:
            censored.append(failure)
            continue
        assert rows is not None
        bars = [
            Bar(
                float(r["raw_open"]),
                float(r["raw_high"]),
                float(r["raw_low"]),
                float(r["raw_close"]),
            )
            for r in rows
        ]
        day_index = {r["date"]: i for i, r in enumerate(rows)}
        facts: list[Any] | None = (
            [row.get(_LIMIT_FACT_COLUMN) for row in rows] if limit_fact_present else None
        )
        facts_valid = facts is not None and all(
            isinstance(f, (int, float)) and math.isfinite(float(f)) and float(f) > 0 for f in facts
        )
        if limit_fact_present and not facts_valid:
            baseline_fact_incomplete = True
            censored.append({"symbol": symbol, "code": "limit_fact_invalid"})
            facts = None
        volumes = (
            [float(r["raw_volume"]) for r in rows]
            if volume_available
            and all(
                isinstance(r.get("raw_volume"), (int, float))
                and math.isfinite(float(r["raw_volume"]))
                and float(r["raw_volume"]) >= 0
                for r in rows
            )
            else None
        )
        sealed_flags: list[bool | None]
        if facts_valid:
            sealed_flags = [_fact_sealed(bar.close, facts[index]) for index, bar in enumerate(bars)]
        else:
            sealed_flags = [None] * len(bars)
        for i, bar in enumerate(bars):
            if not is_single_yang(bar):
                continue
            coverage["pattern"]["yang_anchors_scanned"] += 1
            if i + DEFAULT_WINDOW >= len(bars):
                continue
            follow = bars[i + 1 : i + DEFAULT_WINDOW + 1]
            if any(item.low < bar.low for item in follow):
                coverage["pattern"]["structure_break"] += 1
                continue
            anchor_day = rows[i]["date"]
            anchor_position = calendar_pos.get(anchor_day)
            expected_follow = (
                calendar[anchor_position + 1 : anchor_position + DEFAULT_WINDOW + 1]
                if anchor_position is not None
                else []
            )
            if (
                len(expected_follow) != DEFAULT_WINDOW
                or [r["date"] for r in rows[i + 1 : i + DEFAULT_WINDOW + 1]] != expected_follow
            ):
                censored.append(
                    {
                        "symbol": symbol,
                        "code": "confirmation_window_incomplete",
                        "anchor_date": anchor_day,
                    }
                )
                continue
            confirm_day = expected_follow[-1]
            if anchor_day < start or confirm_day > end:
                continue
            confirm_pos = calendar_pos[confirm_day]
            if confirm_pos + 1 >= len(calendar):
                censored.append(
                    {
                        "symbol": symbol,
                        "code": "next_market_day_unavailable",
                        "anchor_date": anchor_day,
                    }
                )
                continue
            entry_day = calendar[confirm_pos + 1]
            entry_idx = day_index.get(entry_day)
            if entry_idx is None:
                censored.append(
                    {"symbol": symbol, "code": "forward_bar_missing", "anchor_date": anchor_day}
                )
                continue
            raw_entry_open = float(rows[entry_idx]["raw_open"])
            entry_seal = (
                _fact_sealed(raw_entry_open, facts[entry_idx]) if facts is not None else None
            )
            entry_price = float(rows[entry_idx]["open"])
            event: dict[str, Any] = {
                "symbol": symbol,
                "anchor_date": anchor_day,
                "confirm_date": confirm_day,
                "available_from": entry_day,
                "entry_date": entry_day,
                "entry_price": entry_price,
                "raw_entry_open": raw_entry_open,
                "segment": "oos" if entry_day >= oos_start else "is",
                "entry_reachable": None if entry_seal is None else not entry_seal,
                "evidence": {
                    "raw_open": bar.open,
                    "raw_high": bar.high,
                    "raw_low": bar.low,
                    "raw_close": bar.close,
                    "follow_lows": [item.low for item in follow],
                },
                "holdings": [],
            }
            if volumes is not None and volumes[i] > 0:
                ratio = sum(volumes[i + 1 : i + DEFAULT_WINDOW + 1]) / (volumes[i] * DEFAULT_WINDOW)
                event["evidence"]["volume_contraction_ratio"] = ratio
                if ratio <= 1.0:
                    coverage["pattern"]["volume_subtyping"]["volume_contracted"] = (
                        coverage["pattern"]["volume_subtyping"].get("volume_contracted", 0) + 1
                    )
                else:
                    coverage["pattern"]["volume_subtyping"]["volume_not_contracted"] = (
                        coverage["pattern"]["volume_subtyping"].get("volume_not_contracted", 0) + 1
                    )
            coverage["pattern"]["events_detected"] += 1
            if event["entry_reachable"] is False:
                coverage["pattern"]["entry_unreachable_sealed"] += 1
            elif event["entry_reachable"] is None:
                coverage["pattern"]["entry_reachability_unknown"] += 1
            else:
                coverage["pattern"]["achievable_events"] += 1
            pattern_events.append(event)
            segment = event["segment"]
            entry_pos = confirm_pos + 1
            for h in horizons:
                target_pos = entry_pos + h
                target_idx = (
                    day_index.get(calendar[target_pos]) if target_pos < len(calendar) else None
                )
                bucket = coverage["horizons"]["pattern"][segment][str(h)]
                if target_idx is None:
                    bucket["censored"] += 1
                    continue
                bucket["complete"] += 1
                if event["entry_reachable"] is True:
                    gross = float(rows[target_idx]["close"]) / entry_price - 1.0
                    net = gross - cost_bps / 10000.0
                    event["holdings"].append(
                        {"horizon": h, "gross_return": gross, "post_cost_return": net}
                    )
                    returns["pattern"][segment][h].setdefault(symbol, []).append(net)
        if not facts_valid:
            continue
        assert facts is not None
        for i in range(1, len(bars)):
            if sealed_flags[i] is not True or sealed_flags[i - 1] is True:
                continue
            board_day = rows[i]["date"]
            if board_day < start or board_day > end:
                continue
            coverage["baseline"]["first_boards_detected"] += 1
            board_pos = calendar_pos.get(board_day)
            if board_pos is None or board_pos + 1 >= len(calendar):
                censored.append(
                    {
                        "symbol": symbol,
                        "code": "next_market_day_unavailable",
                        "anchor_date": board_day,
                    }
                )
                continue
            entry_day = calendar[board_pos + 1]
            entry_idx = day_index.get(entry_day)
            if entry_idx is None:
                censored.append(
                    {"symbol": symbol, "code": "forward_bar_missing", "anchor_date": board_day}
                )
                continue
            raw_entry_open = float(rows[entry_idx]["raw_open"])
            entry_seal = _fact_sealed(raw_entry_open, facts[entry_idx])
            entry_price = float(rows[entry_idx]["open"])
            event = {
                "symbol": symbol,
                "board_date": board_day,
                "entry_date": entry_day,
                "entry_price": entry_price,
                "raw_entry_open": raw_entry_open,
                "segment": "oos" if entry_day >= oos_start else "is",
                "entry_reachable": None if entry_seal is None else not entry_seal,
                "holdings": [],
            }
            if event["entry_reachable"] is False:
                coverage["baseline"]["entry_unreachable_sealed"] += 1
            elif event["entry_reachable"] is None:
                pass
            else:
                coverage["baseline"]["achievable_events"] += 1
            baseline_events.append(event)
            for h in horizons:
                target_pos = board_pos + 1 + h
                target_idx = (
                    day_index.get(calendar[target_pos]) if target_pos < len(calendar) else None
                )
                bucket = coverage["horizons"]["baseline"][event["segment"]][str(h)]
                if target_idx is None:
                    bucket["censored"] += 1
                    continue
                bucket["complete"] += 1
                if event["entry_reachable"] is True:
                    gross = float(rows[target_idx]["close"]) / entry_price - 1.0
                    net = gross - cost_bps / 10000.0
                    event["holdings"].append(
                        {"horizon": h, "gross_return": gross, "post_cost_return": net}
                    )
                    returns["baseline"][event["segment"]][h].setdefault(symbol, []).append(net)
    for arm, events, day_key in (
        ("pattern", pattern_events, "available_from"),
        ("baseline", baseline_events, "entry_date"),
    ):
        positions_by_symbol: dict[str, list[int]] = {}
        for event in events:
            position = calendar_pos.get(event[day_key])
            if position is not None:
                positions_by_symbol.setdefault(event["symbol"], []).append(position)
        totals = coverage["overlap"][arm]
        for positions in positions_by_symbol.values():
            ordered = sorted(positions)
            totals["events"] += len(ordered)
            totals["overlapping_events"] += sum(
                1 for previous, current in pairwise(ordered) if current - previous < _OVERLAP_GAP
            )
    arms: dict[str, Any] = {}
    for arm, events in (("pattern", pattern_events), ("baseline", baseline_events)):
        arms[arm] = {"status": "ok", "reasons": [], "events": events, "segments": {}}
        for segment in ("is", "oos"):
            arms[arm]["segments"][segment] = {
                "horizons": {
                    str(h): _simple_stats(
                        [value for values in returns[arm][segment][h].values() for value in values],
                        set(returns[arm][segment][h]),
                    )
                    for h in horizons
                },
            }
    if baseline_fact_incomplete:
        arms["baseline"]["status"] = "unavailable"
        arms["baseline"]["reasons"] = ["limit_up_price_fact_missing"]
        arms["baseline"]["events"] = []
        arms["baseline"]["segments"] = {}
    comparison: dict[str, Any] = {}
    for h in horizons:
        comparison[str(h)] = {}
        for segment in ("is", "oos"):
            left, right = returns["pattern"][segment][h], returns["baseline"][segment][h]
            boot = selection_cluster_bootstrap(left, right)
            reasons = []
            if baseline_fact_incomplete:
                reasons.append("baseline_limit_up_price_fact_missing")
            if not gates(sum(map(len, left.values())), len(left)):
                reasons.append("pattern_sample_below_gate")
            if not gates(sum(map(len, right.values())), len(right)):
                reasons.append("baseline_sample_below_gate")
            if boot.valid_replicates < MIN_VALID_BOOTSTRAP_REPLICATES:
                reasons.append("bootstrap_min_valid_replicates")
            gate = not reasons
            comparison[str(h)][segment] = {
                "gate": gate,
                "gate_reasons": reasons,
                "pattern_n": sum(map(len, left.values())),
                "baseline_n": sum(map(len, right.values())),
                "bootstrap": {
                    "mean_difference": boot.mean_difference,
                    "ci_lower": boot.lower,
                    "ci_upper": boot.upper,
                    "valid_replicates": boot.valid_replicates,
                    "rounds": boot.rounds,
                },
                "verdict": "accepted"
                if gate and boot.lower is not None and boot.lower > 0
                else ("rejected" if gate else "unavailable"),
            }
    if baseline_fact_incomplete:
        verdict = {"value": "unavailable", "reasons": ["baseline_limit_up_price_fact_missing"]}
    else:
        ungated = [
            h
            for h in PRIMARY_HORIZONS
            if h not in horizons or not comparison[str(h)]["oos"]["gate"]
        ]
        if ungated:
            verdict = {
                "value": "unavailable",
                "reasons": [f"primary_oos_horizon_{h}_not_gated" for h in ungated],
            }
        elif all(comparison[str(h)]["oos"]["bootstrap"]["ci_lower"] > 0 for h in PRIMARY_HORIZONS):
            verdict = {
                "value": "accepted",
                "reasons": ["all_primary_oos_horizons_ci_lower_positive"],
            }
        else:
            verdict = {"value": "rejected", "reasons": ["primary_oos_increment_not_stable"]}
    provenance = {
        "generation": generation,
        "manifest_sha256": manifest_hash.lower(),
        "oos_start": oos_start,
        "cost_bps": cost_bps,
        "hold_horizons": list(horizons),
        "limit_fact_column_present": limit_fact_present,
        "pristine_oos": False,
        "oos_nature": "expanded_generation_pinned_sample_not_pristine",
    }
    source_provenance = getattr(reader, "source_provenance", None)
    if callable(source_provenance):
        provenance["sources"] = source_provenance()
    return {
        "status": "ok",
        "research_id": INCREMENT_RESEARCH_ID,
        "reasons": [],
        "definition": {
            "increment": dict(INCREMENT_DEFINITION),
            "pattern": dict(SINGLE_YANG_DEFINITION),
        },
        "provenance": provenance,
        "arms": arms,
        "coverage": coverage,
        "censored": censored,
        "comparison": comparison,
        "verdict": verdict,
    }


__all__ = [
    "DEFAULT_HOLD_HORIZONS",
    "DEFAULT_WINDOW",
    "INCREMENT_DEFINITION",
    "INCREMENT_RESEARCH_ID",
    "MIN_BODY_PCT_OF_OPEN",
    "PRIMARY_HORIZONS",
    "RESEARCH_ID",
    "SINGLE_YANG_DEFINITION",
    "Bar",
    "SingleYangCompositeReader",
    "assess_capability",
    "detect_single_yang",
    "evaluate_single_yang",
    "evaluate_single_yang_increment",
    "is_single_yang",
    "run_single_yang_research",
]
