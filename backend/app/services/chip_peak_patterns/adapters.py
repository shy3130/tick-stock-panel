"""Injectable Protocols for sealed bars, PIT turnover, market facts and presence."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol, runtime_checkable

from .models import REQUIRED_CANONICAL_COLUMNS, ChipBar, TurnoverDay


@runtime_checkable
class SealedDailyBars(Protocol):
    def identity(self) -> object: ...
    def market_days(self, start: date, end: date) -> Sequence[date]: ...
    def load_bars(self, symbol: str, start: date, end: date) -> Sequence[ChipBar]: ...


@runtime_checkable
class PitTurnover(Protocol):
    def identity(self) -> object: ...
    def turnover(self, symbol: str, day: date) -> TurnoverDay | None: ...


@runtime_checkable
class MarketFacts(Protocol):
    def identity(self) -> object: ...
    def row(self, symbol: str, day: date) -> object | None: ...


@runtime_checkable
class Presence(Protocol):
    def identity(self) -> object: ...
    def membership(self, symbol: str, day: date) -> str: ...


@dataclass(frozen=True)
class ChipReaderBundle:
    bars: SealedDailyBars
    turnover: PitTurnover
    market_facts: MarketFacts
    presence: Presence


def request_windows(
    reader: SealedDailyBars, start: date, end: date, *, warmup: int = 250, forward: int = 80
) -> tuple[tuple[date, ...], date, date]:
    days = tuple(reader.market_days(start - timedelta(days=500), end + timedelta(days=180)))
    if not days:
        return (), start, end
    first = next((i for i, d in enumerate(days) if d >= start), len(days))
    last = next((i for i in range(len(days) - 1, -1, -1) if days[i] <= end), first - 1)
    return days, days[max(0, first - warmup)], days[min(len(days) - 1, last + forward)]


def frame_to_chip_bars(frame: Any, symbol: str) -> tuple[ChipBar, ...]:
    if frame is None or getattr(frame, "is_empty", lambda: True)():
        return ()
    required = set(REQUIRED_CANONICAL_COLUMNS)
    columns = set(getattr(frame, "columns", ()))
    if not required <= columns:
        raise ValueError("canonical columns missing")
    rows = frame.sort("date").iter_rows(named=True)
    return tuple(
        ChipBar(
            symbol=symbol,
            date=row["date"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            raw_open=float(row["raw_open"]),
            raw_high=float(row["raw_high"]),
            raw_low=float(row["raw_low"]),
            raw_close=float(row["raw_close"]),
            volume=float(row["volume"]),
            amount=float(row["amount"]),
        )
        for row in rows
    )


def resolve_pit_turnover(
    source: PitTurnover, symbol: str, bars: Sequence[ChipBar]
) -> tuple[TurnoverDay | None, ...]:
    return tuple(source.turnover(symbol, bar.date) for bar in bars)


def row_limit_up(row: object | None) -> float | None:
    if row is None:
        return None
    value = getattr(row, "published_limit_up", None)
    try:
        return float(value) if value is not None and math.isfinite(float(value)) else None
    except (TypeError, ValueError):
        return None
