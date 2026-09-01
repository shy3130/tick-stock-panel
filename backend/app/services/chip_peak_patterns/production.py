"""Pinned repository adapters for chip peak pattern evaluation."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import date, datetime
from typing import Any

import polars as pl

from app.data_providers.fquant.daily_market_research import PublishedDailyMarketFactsReader
from app.services.hold_firm_patterns.adapters import (
    PinnedPresenceUniverseReader,
    canonical_identity,
    pinned_market_facts_source,
)
from app.services.research_sealed_data import PublishedCanonicalDailyReader
from app.services.universe_presence_history import (
    PresenceHistoryError,
    PublishedPresenceUniverseReader,
    universe_presence_root,
)

from .adapters import ChipReaderBundle, frame_to_chip_bars, request_windows
from .models import (
    REQUIRED_CANONICAL_COLUMNS,
    ChipBar,
    ChipPeakRequest,
    TurnoverDay,
    UnavailabilityReason,
)

IN_POOL, NOT_IN_POOL, COVERAGE_MISSING = "in_pool", "not_in_pool", "coverage_missing"


class ChipProductionScopeUnavailableError(RuntimeError):
    def __init__(self, reason: UnavailabilityReason, detail: str) -> None:
        super().__init__(detail)
        self.reason, self.detail = reason, detail


def _close_quietly(reader: Any) -> None:
    close = getattr(reader, "close", None)
    if callable(close):
        with suppress(Exception):
            close()


class PinnedChipBars:
    def __init__(self, reader: PublishedCanonicalDailyReader) -> None:
        if not reader.has_columns(*REQUIRED_CANONICAL_COLUMNS):
            raise ChipProductionScopeUnavailableError(
                UnavailabilityReason.CANONICAL_READER, "canonical generation lacks required columns"
            )
        self._reader, self._identity = reader, canonical_identity(reader)

    def identity(self) -> object:
        return self._identity

    def market_days(self, start: date, end: date) -> tuple[date, ...]:
        return tuple(self._reader.market_days(start, end))

    def load_bars(self, symbol: str, start: date, end: date) -> tuple[ChipBar, ...]:
        frame: pl.DataFrame = self._reader.daily_bars(symbol, start, end)
        if not frame.is_empty() and frame.schema.get("date") != pl.Date:
            frame = frame.with_columns(pl.col("date").cast(pl.Date))
        return frame_to_chip_bars(frame, symbol)


class PinnedChipTurnover:
    def __init__(self, reader: PublishedDailyMarketFactsReader) -> None:
        self._reader = reader
        self._identity: Mapping[str, str] = {
            "source": "published_daily_markets_hslv_or_lagged_ltgb",
            "generation": str(reader.generation()),
            "manifest_sha256": str(reader.manifest_sha256()),
        }

    def identity(self) -> Mapping[str, str]:
        return self._identity

    @staticmethod
    def _available_day(value: Any) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return None

    def turnover(self, symbol: str, day: date) -> TurnoverDay | None:
        reported = self._reader.daily_turnover_fact(symbol, day)
        if reported is not None:
            return TurnoverDay(
                available_at=self._available_day(reported.available_at),
                reported_turnover_pct=reported.reported_turnover_pct,
                source_day=reported.source_day,
                availability_basis=reported.availability_basis,
            )
        lagged = self._reader.intraday_float_shares_fact(symbol, day)
        if lagged is None:
            return None
        return TurnoverDay(
            available_at=self._available_day(lagged.available_at),
            float_shares=lagged.float_shares,
            source_day=lagged.source_day,
            availability_basis=lagged.availability_basis,
        )


class PinnedChipPresence(PinnedPresenceUniverseReader):
    def membership(self, symbol: str, day: date) -> str:
        pool = self._pools.get(day)
        if pool is None:
            return COVERAGE_MISSING
        return IN_POOL if symbol in pool else NOT_IN_POOL


@contextmanager
def production_reader_scope(repo: Any, request: ChipPeakRequest) -> Iterator[ChipReaderBundle]:
    """Yield a request-pinned production bundle and close markets on exit."""
    canonical_reader = PublishedCanonicalDailyReader.from_repository(repo)
    if canonical_reader is None:
        raise ChipProductionScopeUnavailableError(
            UnavailabilityReason.CANONICAL_READER, "canonical history is not published"
        )
    bars = PinnedChipBars(canonical_reader)
    try:
        facts_reader = PublishedDailyMarketFactsReader.from_canonical_manifest(
            canonical_reader.manifest()
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ChipProductionScopeUnavailableError(
            UnavailabilityReason.MARKET_FACTS_INCOMPLETE,
            f"pinned markets generation unavailable: {exc}",
        ) from exc
    try:
        days, _, _ = request_windows(bars, request.start, request.end)
        event_days = tuple(day for day in days if request.start <= day <= request.end)
        try:
            facts = pinned_market_facts_source(facts_reader, request.symbols, days)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ChipProductionScopeUnavailableError(
                UnavailabilityReason.MARKET_FACTS_INCOMPLETE,
                f"pinned markets facts prefetch failed: {exc}",
            ) from exc
        try:
            data_dir = getattr(getattr(repo, "store", None), "data_dir", None)
            presence = PinnedChipPresence(
                PublishedPresenceUniverseReader(universe_presence_root(), data_dir=data_dir),
                event_days,
            )
        except (OSError, PresenceHistoryError, RuntimeError, TypeError, ValueError) as exc:
            raise ChipProductionScopeUnavailableError(
                UnavailabilityReason.UNIVERSE_PRESENCE, f"universe presence unavailable: {exc}"
            ) from exc
        yield ChipReaderBundle(
            bars=bars,
            turnover=PinnedChipTurnover(facts_reader),
            market_facts=facts,
            presence=presence,
        )
    finally:
        _close_quietly(facts_reader)


__all__ = [
    "COVERAGE_MISSING",
    "IN_POOL",
    "NOT_IN_POOL",
    "ChipProductionScopeUnavailableError",
    "PinnedChipBars",
    "PinnedChipPresence",
    "PinnedChipTurnover",
    "production_reader_scope",
]
