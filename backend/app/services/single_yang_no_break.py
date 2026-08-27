"""Auditable single-yang-no-break research over one immutable raw generation."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol, Sequence

RESEARCH_ID = "single_yang_no_break_v1"
DEFAULT_WINDOW = 5
MIN_BODY_PCT_OF_OPEN = 0.02
_REQUIRED_READER_METHODS = (
    "generation", "manifest_sha256", "market_days", "daily_bars", "columns"
)
_REQUIRED_RAW_COLUMNS = ("raw_open", "raw_high", "raw_low", "raw_close")

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
        return {"available": False, "reasons": ["raw_generation_columns_missing"], "missing_columns": missing}
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
        missing = [column for column in ("date", *_REQUIRED_RAW_COLUMNS) if column not in frame.columns]
        if missing:
            censored.append({"symbol": symbol, "code": "raw_field_missing", "fields": missing})
            continue
        rows: list[dict[str, Any]] = []
        bad = False
        for row in frame.sort("date").to_dicts():
            values = [row.get(column) for column in _REQUIRED_RAW_COLUMNS]
            if any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0 for value in values):
                censored.append({"symbol": symbol, "code": "raw_field_invalid", "date": str(row.get("date"))})
                bad = True
                break
            row_generation = row.get("generation")
            if row_generation is not None and row_generation != generation:
                censored.append({"symbol": symbol, "code": "generation_mismatch", "date": str(row.get("date"))})
                bad = True
                break
            rows.append(row)
        if bad:
            continue
        bars = [Bar(float(row["raw_open"]), float(row["raw_high"]), float(row["raw_low"]), float(row["raw_close"])) for row in rows]
        for anchor_index in detect_single_yang(bars):
            anchor_day = rows[anchor_index]["date"]
            anchor_position = calendar_pos.get(anchor_day)
            if anchor_position is None:
                censored.append({
                    "symbol": symbol,
                    "code": "anchor_calendar_date_missing",
                    "anchor_date": anchor_day,
                })
                continue
            expected_follow_days = calendar[
                anchor_position + 1 : anchor_position + DEFAULT_WINDOW + 1
            ]
            if len(expected_follow_days) != DEFAULT_WINDOW:
                censored.append({
                    "symbol": symbol,
                    "code": "confirmation_window_truncated",
                    "anchor_date": anchor_day,
                })
                continue
            confirm_index = anchor_index + DEFAULT_WINDOW
            actual_follow_days = [
                row["date"] for row in rows[anchor_index + 1 : confirm_index + 1]
            ]
            if actual_follow_days != expected_follow_days:
                censored.append({
                    "symbol": symbol,
                    "code": "confirmation_window_incomplete",
                    "anchor_date": anchor_day,
                    "missing_market_days": [
                        day for day in expected_follow_days if day not in actual_follow_days
                    ],
                })
                continue
            confirm_day = expected_follow_days[-1]
            if anchor_day < start or confirm_day > end:
                continue
            position = calendar_pos.get(confirm_day)
            if position is None or position + 1 >= len(calendar):
                censored.append({"symbol": symbol, "code": "next_market_day_unavailable", "anchor_date": anchor_day})
                continue
            available_from = calendar[position + 1]
            next_row = next((row for row in rows if row["date"] == available_from), None)
            if next_row is None:
                censored.append({"symbol": symbol, "code": "forward_bar_missing", "anchor_date": anchor_day})
                continue
            anchor = bars[anchor_index]
            follow = bars[anchor_index + 1 : confirm_index + 1]
            gross = float(next_row["raw_close"]) / bars[confirm_index].close - 1.0
            body_ratio = (Decimal(str(anchor.close)) - Decimal(str(anchor.open))) / Decimal(str(anchor.open))
            events.append({
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
            })

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


__all__ = [
    "RESEARCH_ID", "DEFAULT_WINDOW", "MIN_BODY_PCT_OF_OPEN",
    "SINGLE_YANG_DEFINITION", "Bar", "is_single_yang", "detect_single_yang",
    "assess_capability", "evaluate_single_yang", "run_single_yang_research",
]
