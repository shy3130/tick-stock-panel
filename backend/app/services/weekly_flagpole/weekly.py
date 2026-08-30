"""Deterministic daily-to-weekly OHLCV aggregation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True, slots=True)
class WeeklyBar:
    symbol: str
    week_key: date
    first_day: date
    last_day: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    bar_days: int
    complete: bool


def _week(d: date) -> tuple[int, int]:
    iso = d.isocalendar()
    return int(iso.year), int(iso.week)


def bars_to_dicts(
    rows: Iterable[dict[str, Any]], symbol: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    result = sorted((dict(row) for row in rows), key=lambda row: row.get("date"))
    required = ("date", "raw_open", "raw_high", "raw_low", "raw_close", "volume")
    for row in result:
        missing = [field for field in required if field not in row or row[field] is None]
        if missing:
            return [], {
                "symbol": symbol,
                "code": "raw_field_missing",
                "detail": {"fields": missing},
            }
        for field in required[1:]:
            try:
                value = float(row[field])
            except (TypeError, ValueError):
                value = float("nan")
            if (
                value != value
                or value == float("inf")
                or value == float("-inf")
                or (field != "date" and value <= 0)
            ):
                return [], {
                    "symbol": symbol,
                    "code": "raw_field_invalid",
                    "detail": {"field": field, "date": str(row["date"])},
                }
    return result, None


def aggregate_weekly_bars(
    *, symbol: str, rows: Iterable[dict[str, Any]], market_days: Iterable[date], window_end: date
) -> list[WeeklyBar]:
    calendar = sorted(set(market_days))
    last_window = max((d for d in calendar if d <= window_end), default=None)
    if last_window is None:
        return []
    scheduled: dict[tuple[int, int], list[date]] = defaultdict(list)
    for day in calendar:
        scheduled[_week(day)].append(day)
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        day = row.get("date")
        if isinstance(day, date) and day <= window_end:
            grouped[_week(day)].append(row)
    output: list[WeeklyBar] = []
    for key, group in grouped.items():
        group.sort(key=lambda row: row["date"])
        in_window = [d for d in scheduled.get(key, ()) if d <= last_window]
        week_key = max(in_window, default=group[-1]["date"])
        complete = bool(scheduled.get(key)) and max(scheduled[key]) <= last_window
        output.append(
            WeeklyBar(
                symbol,
                week_key,
                group[0]["date"],
                group[-1]["date"],
                float(group[0]["raw_open"]),
                max(float(r["raw_high"]) for r in group),
                min(float(r["raw_low"]) for r in group),
                float(group[-1]["raw_close"]),
                sum(float(r["volume"]) for r in group),
                len(group),
                complete,
            )
        )
    trailing_key = _week(last_window)
    if trailing_key not in grouped and any(_week(day) == trailing_key for day in calendar):
        output.append(
            WeeklyBar(
                symbol,
                last_window,
                last_window,
                last_window,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0,
                False,
            )
        )
    return sorted(output, key=lambda bar: bar.week_key)
