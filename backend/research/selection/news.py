"""Point-in-time news overlay contract.

The project currently has no historical news archive.  This module defines the
strict boundary now so a later data feed cannot accidentally leak future news
into historical selection.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable


REQUIRED_FIELDS = frozenset({"published_at", "ts_code", "source", "event_type", "score"})


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("published_at must be an ISO-8601 string or datetime")
    if parsed.tzinfo is None:
        raise ValueError("published_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_news_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for index, raw in enumerate(records):
        missing = sorted(REQUIRED_FIELDS - raw.keys())
        if missing:
            raise ValueError(f"news record {index} missing fields: {', '.join(missing)}")
        score = float(raw["score"])
        if not -1.0 <= score <= 1.0:
            raise ValueError(f"news record {index} score must be in [-1, 1]")
        symbol = str(raw["ts_code"]).strip().upper()
        if not symbol:
            raise ValueError(f"news record {index} has empty ts_code")
        validated.append({
            **raw,
            "ts_code": symbol,
            "score": score,
            "published_at_utc": _parse_timestamp(raw["published_at"]),
        })
    return validated


def news_scores_as_of(
    records: Iterable[dict[str, Any]],
    *,
    as_of: datetime,
    lookback_days: int = 5,
) -> dict[str, dict[str, Any]]:
    """Aggregate only events that were public by ``as_of``.

    Scores are source-deduplicated by ``(symbol, source, event_type, published_at)``
    and averaged. Future publications and events outside the lookback are ignored.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")
    cutoff = as_of.astimezone(timezone.utc)
    lower = cutoff - timedelta(days=int(lookback_days))
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str, str, datetime]] = set()
    for row in validate_news_records(records):
        published = row["published_at_utc"]
        if published > cutoff or published < lower:
            continue
        key = (
            row["ts_code"],
            str(row["source"]),
            str(row["event_type"]),
            published,
        )
        if key in seen:
            continue
        seen.add(key)
        grouped[row["ts_code"]].append(row)

    result: dict[str, dict[str, Any]] = {}
    for symbol, rows in sorted(grouped.items()):
        result[symbol] = {
            "score": sum(float(row["score"]) for row in rows) / len(rows),
            "event_count": len(rows),
            "latest_published_at": max(row["published_at_utc"] for row in rows).isoformat(),
            "event_types": sorted({str(row["event_type"]) for row in rows}),
        }
    return result


def trading_day_as_of(day: date) -> datetime:
    """Conservative daily cut-off: previous UTC day end (08:00 China time)."""
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
