"""Request-level pinned adapters for Issue #38 hold-firm-pattern research.

Binds the shared published readers (canonical history, markets facts, PIT
presence history) to the frozen ``models`` protocols for the lifetime of one
request.  Nothing here follows ``current`` after construction, nothing
fabricates ``suspended``/``buyable``/``sellable`` state, and every identity
failure fails closed into an order-level unavailability reason.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterator, Mapping, Sequence

import polars as pl

from app.data_providers.fquant.daily_market_research import (
    PinnedMarketFacts,
    PublishedDailyMarketFactsReader,
)
from app.services.hold_firm_patterns.models import (
    REQUIRED_CANONICAL_COLUMNS,
    Bar,
    CanonicalIdentity,
    MarketFactsIdentity,
    MarketFactsRow,
    PitUniverseStatus,
    UnavailabilityReason,
    UniverseDayIdentity,
    UniverseIdentity,
)
from app.services.research_sealed_data import PublishedCanonicalDailyReader
from app.services.universe_presence_history import (
    PresenceHistoryError,
    PresenceStatus,
    PublishedPresenceUniverseReader,
)
from app.services.universe_scd import canonical_json_bytes, sha256_hex

# Market-day budgets around the request window.  The widest detector lookback
# is the 120-day low-position window (F3/F4) on top of a 20-day platform/slope
# window; the forward side must cover the F2 five-day selection window plus the
# common 20-day horizon and its exit search.
WARMUP_MARKET_DAYS = 160
POST_HORIZON_MARKET_DAYS = 40
# Calendar-day spans used to over-fetch so the market-day budgets above can
# always be satisfied from the pinned calendar (~250 trading days per year).
LOOKBACK_CALENDAR_DAYS = 400
FORWARD_CALENDAR_DAYS = 120


class ProductionReaderScopeUnavailable(RuntimeError):
    """Raised when the pinned production reader stack cannot be constructed."""

    def __init__(self, reason: UnavailabilityReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass
class ProductionReaderScope:
    """Request-scoped bundle of pinned production readers."""

    canonical: PublishedCanonicalDailyReader
    market_facts: PublishedDailyMarketFactsReader
    universe_reader: PublishedPresenceUniverseReader
    repo: Any | None = None

    def close(self) -> None:
        close = getattr(self.market_facts, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - shutdown best effort
                pass

    def __enter__(self) -> "ProductionReaderScope":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@contextmanager
def production_reader_scope(repo: Any) -> Iterator[ProductionReaderScope]:
    """Open the pinned canonical/markets/universe stack for one request.

    Fails closed with :class:`ProductionReaderScopeUnavailable` carrying the
    models ``UnavailabilityReason``; the caller converts that into an
    order-level unavailable response.  The markets reader is closed on exit.
    """
    canonical_reader = PublishedCanonicalDailyReader.from_repository(repo)
    if canonical_reader is None:
        raise ProductionReaderScopeUnavailable(
            UnavailabilityReason.CANONICAL_READER, "canonical history is not published"
        )
    if not canonical_reader.has_columns(*REQUIRED_CANONICAL_COLUMNS):
        raise ProductionReaderScopeUnavailable(
            UnavailabilityReason.CANONICAL_READER,
            "canonical generation lacks required columns",
        )
    try:
        facts_reader = PublishedDailyMarketFactsReader.from_canonical_manifest(
            canonical_reader.manifest()
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ProductionReaderScopeUnavailable(
            UnavailabilityReason.MARKET_FACTS_INCOMPLETE,
            f"pinned markets generation unavailable: {exc}",
        ) from exc
    try:
        universe_reader = getattr(repo, "pit_presence_universe", None)
        if universe_reader is None:
            raise RuntimeError("pinned universe presence reader unavailable")
    except (OSError, PresenceHistoryError, RuntimeError, ValueError) as exc:
        facts_reader.close()
        raise ProductionReaderScopeUnavailable(
            UnavailabilityReason.UNIVERSE_PRESENCE,
            f"universe presence unavailable: {exc}",
        ) from exc
    scope = ProductionReaderScope(
        canonical=canonical_reader,
        market_facts=facts_reader,
        universe_reader=universe_reader,
        repo=repo,
    )
    try:
        yield scope
    finally:
        scope.close()


def pinned_markets_generation(canonical_manifest: Mapping[str, Any]) -> str | None:
    """Flatten ``source_generations["markets"]`` to a generation string."""
    sources = canonical_manifest.get("source_generations")
    pinned = sources.get("markets") if isinstance(sources, Mapping) else None
    if isinstance(pinned, Mapping):
        generation = pinned.get("generation")
    else:
        generation = pinned
    return generation if isinstance(generation, str) and generation else None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def canonical_identity(reader: PublishedCanonicalDailyReader) -> CanonicalIdentity:
    """Build the frozen canonical identity from the pinned reader."""
    manifest = reader.manifest()
    raw_sources = manifest.get("source_generations")
    source_generations: dict[str, str] = {}
    if isinstance(raw_sources, Mapping):
        for logical, pinned in raw_sources.items():
            if isinstance(pinned, Mapping):
                generation = pinned.get("generation")
            else:
                generation = pinned
            if isinstance(generation, str) and generation:
                source_generations[str(logical)] = generation
    if not source_generations:
        raise ValueError("canonical manifest lacks source_generations")
    calendar_parts: list[str] = []
    raw_calendar = manifest.get("calendar_source_generations")
    if isinstance(raw_calendar, Mapping):
        for logical in sorted(raw_calendar):
            pinned = raw_calendar[logical]
            generation = pinned.get("generation") if isinstance(pinned, Mapping) else pinned
            digest = pinned.get("manifest_sha256") if isinstance(pinned, Mapping) else None
            if isinstance(generation, str) and generation:
                suffix = f":{str(digest)[:16]}" if isinstance(digest, str) and digest else ""
                calendar_parts.append(f"{logical}={generation}{suffix}")
    calendar_id = (
        "canonical_calendar/" + ";".join(calendar_parts)
        if calendar_parts
        else f"canonical/{reader.generation()}"
    )
    return CanonicalIdentity(
        generation=reader.generation(),
        manifest_sha256=reader.manifest_sha256(),
        source_generations=source_generations,
        calendar_id=calendar_id,
    )


class PinnedCanonicalDailyReader:
    """models.CanonicalDailyReader over the published canonical generation."""

    def __init__(self, reader: PublishedCanonicalDailyReader) -> None:
        if not reader.has_columns(*REQUIRED_CANONICAL_COLUMNS):
            raise ValueError("canonical generation lacks required columns")
        self._reader = reader
        self._identity = canonical_identity(reader)

    def identity(self) -> CanonicalIdentity:
        return self._identity

    def trading_days(self, start: date, end: date) -> tuple[date, ...]:
        return tuple(self._reader.market_days(start, end))

    def load_bars(self, symbol: str, start: date, end: date) -> tuple[Bar, ...]:
        frame: pl.DataFrame = self._reader.daily_bars(symbol, start, end)
        if frame.is_empty():
            return ()
        bars: list[Bar] = []
        for row in frame.sort("date").iter_rows(named=True):
            day = row.get("date")
            if hasattr(day, "date") and not isinstance(day, date):
                day = day.date()
            if not isinstance(day, date):
                continue
            values = {
                name: _finite(row.get(name))
                for name in (
                    "open",
                    "high",
                    "low",
                    "close",
                    "raw_open",
                    "raw_high",
                    "raw_low",
                    "raw_close",
                    "volume",
                    "amount",
                )
            }
            adjusted = (values["open"], values["high"], values["low"], values["close"])
            raw = (
                values["raw_open"],
                values["raw_high"],
                values["raw_low"],
                values["raw_close"],
            )
            if any(value is None for value in adjusted + raw):
                continue
            volume = values["volume"]
            amount = values["amount"]
            bars.append(
                Bar(
                    symbol=symbol,
                    date=day,
                    research_open_adj=values["open"],
                    research_high_adj=values["high"],
                    research_low_adj=values["low"],
                    research_close_adj=values["close"],
                    quote_open_raw=values["raw_open"],
                    quote_high_raw=values["raw_high"],
                    quote_low_raw=values["raw_low"],
                    quote_close_raw=values["raw_close"],
                    volume=volume if volume is not None else 0.0,
                    amount=amount if amount is not None else 0.0,
                )
            )
        return tuple(bars)


class PinnedMarketFactsSource:
    """models.MarketFactsSource over the pinned markets bundle.

    Rows are converted once; any fact that is missing raw OHLC, pre_close,
    published bands, regime, is_st or name converts to ``None`` so callers
    fail closed.  ``suspended``/``buyable``/``sellable`` are never read.
    """

    def __init__(self, generation: str, manifest_sha256: str) -> None:
        self._identity = MarketFactsIdentity(generation=generation, manifest_sha256=manifest_sha256)
        self._rows: dict[tuple[str, date], MarketFactsRow] = {}
        self.incomplete_rows = 0

    @classmethod
    def from_bundle(cls, bundle: PinnedMarketFacts) -> "PinnedMarketFactsSource":
        source = cls(bundle.generation, bundle.manifest_sha256)
        for (symbol, day), fact in bundle.rows.items():
            row = _convert_fact(symbol, day, fact)
            if row is None:
                source.incomplete_rows += 1
                continue
            source._rows[(symbol, day)] = row
        return source

    def identity(self) -> MarketFactsIdentity:
        return self._identity

    def row(self, symbol: str, day: date) -> MarketFactsRow | None:
        return self._rows.get((symbol, day))

    def covers(self, symbol: str, day: date) -> bool:
        return (symbol, day) in self._rows


def pinned_market_facts_source(
    market_facts: object,
    symbols: Sequence[str],
    days: Sequence[date],
) -> PinnedMarketFactsSource:
    """Freeze and normalize exact-day PIT market facts for one request."""
    if isinstance(market_facts, PinnedMarketFacts):
        bundle = market_facts
    else:
        rows: dict[tuple[str, date], object] = {}
        if days:
            start, end = min(days), max(days)
            load = getattr(market_facts, "limit_band_facts")
            for symbol in symbols:
                for day, fact in load(symbol, start, end).items():
                    rows[(symbol, day)] = fact
        bundle = PinnedMarketFacts(
            generation=str(getattr(market_facts, "generation")()),
            manifest_sha256=str(getattr(market_facts, "manifest_sha256")()),
            rows=rows,
        )
    return PinnedMarketFactsSource.from_bundle(bundle)


def _convert_fact(symbol: str, day: date, fact: Any) -> MarketFactsRow | None:
    raw = tuple(
        _finite(getattr(fact, name, None))
        for name in (
            "raw_open",
            "raw_high",
            "raw_low",
            "raw_close",
        )
    )
    pre_close = _finite(getattr(fact, "pre_close", None))
    limit_up = _finite(getattr(fact, "published_limit_up", None))
    limit_down = _finite(getattr(fact, "published_limit_down", None))
    regime = getattr(fact, "regime", None)
    is_st = getattr(fact, "is_st", None)
    name = getattr(fact, "name", None)
    if (
        any(value is None for value in raw)
        or pre_close is None
        or limit_up is None
        or limit_down is None
        or not isinstance(regime, str)
        or not regime
        or not isinstance(is_st, bool)
        or not isinstance(name, str)
        or not name
    ):
        return None
    return MarketFactsRow(
        symbol=symbol,
        date=day,
        quote_open_raw=raw[0],
        quote_high_raw=raw[1],
        quote_low_raw=raw[2],
        quote_close_raw=raw[3],
        pre_close=pre_close,
        published_limit_up=limit_up,
        published_limit_down=limit_down,
        regime=regime,
        is_st=is_st,
        name=name,
    )


def presence_universe_identity(
    manifest: Mapping[str, Any],
    day_identities: Sequence[UniverseDayIdentity] = (),
) -> UniverseIdentity:
    """Validate and freeze retrospective presence provenance."""
    schema_version = manifest.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 2
    ):
        raise ValueError("presence manifest schema_version must be integer 2")
    if manifest.get("artifact") != "universe_presence":
        raise ValueError("presence manifest artifact mismatch")
    if manifest.get("rule_version") != "presence_v1":
        raise ValueError("presence manifest rule_version must be presence_v1")
    if manifest.get("retrospective") is not True:
        raise ValueError("presence manifest must be retrospective")
    status_filter = manifest.get("status_filter")
    if status_filter != "daily_market_row_present_exact_day":
        raise ValueError("presence manifest status_filter mismatch")
    source = manifest.get("source")
    if not isinstance(source, Mapping) or source.get("artifact") != "fstore_snapshot":
        raise ValueError("presence manifest lacks fstore source identity")
    generation = manifest.get("generation")
    source_generation = source.get("generation")
    source_manifest_sha256 = source.get("manifest_sha256")
    if not isinstance(generation, str) or not generation:
        raise ValueError("presence manifest lacks generation")
    if not isinstance(source_generation, str) or not source_generation:
        raise ValueError("presence source lacks generation")
    if not isinstance(source_manifest_sha256, str) or not source_manifest_sha256:
        raise ValueError("presence source lacks manifest_sha256")
    return UniverseIdentity(
        generation=generation,
        manifest_sha256=sha256_hex(canonical_json_bytes(manifest)),
        schema_version=2,
        artifact="universe_presence",
        rule_version="presence_v1",
        retrospective=True,
        status_filter=status_filter,
        source_generation=source_generation,
        source_artifact="fstore_snapshot",
        source_manifest_sha256=source_manifest_sha256,
        day_identities=tuple(day_identities),
    )


class PinnedPresenceUniverseReader:
    """Presence reader pinned to exact requested days; never infers absence."""

    def __init__(
        self, reader: PublishedPresenceUniverseReader, request_days: tuple[date, ...]
    ) -> None:
        days = tuple(sorted(set(request_days)))
        prefetched = reader.prefetch_presence_days(days)
        pools: dict[date, frozenset[str]] = {}
        identities: list[UniverseDayIdentity] = []
        for day in days:
            snapshot = prefetched.get(day)
            if snapshot is None:
                raise PresenceHistoryError(f"presence snapshot missing for {day.isoformat()}")
            status = (
                PresenceStatus.PRESENT
                if snapshot.source_day_observed
                else PresenceStatus.NOT_OBSERVED
            )
            if status is PresenceStatus.NOT_OBSERVED:
                raise PresenceHistoryError(f"presence day {day.isoformat()} is not observed")
            pools[day] = frozenset(snapshot.symbols)
            identities.append(UniverseDayIdentity(day=day, content_hash=snapshot.content_hash))
        self._pools = pools
        self._identity = presence_universe_identity(reader.source_manifest(), identities)

    def identity(self) -> UniverseIdentity:
        return self._identity

    def membership(self, symbol: str, day: date) -> PitUniverseStatus:
        pool = self._pools.get(day)
        if pool is None:
            raise PresenceHistoryError(f"presence snapshot missing for {day.isoformat()}")
        if symbol not in pool:
            raise PresenceHistoryError(
                f"presence cannot prove pool membership for {symbol} on {day.isoformat()}"
            )
        return PitUniverseStatus.IN_POOL


def request_windows(
    reader: PublishedCanonicalDailyReader, start: date, end: date
) -> tuple[tuple[date, ...], tuple[date, ...], date, date]:
    """Split the pinned calendar into full and event windows.

    Returns ``(full_days, event_days, bar_start, bar_end)`` where
    ``full_days`` covers the detector warmup before ``start`` and the
    selection window plus horizon after ``end``, and ``event_days`` is the
    strict ``[start, end]`` slice whose days may anchor parent events.
    """
    full_days = tuple(
        reader.market_days(
            start - timedelta(days=LOOKBACK_CALENDAR_DAYS),
            end + timedelta(days=FORWARD_CALENDAR_DAYS),
        )
    )
    event_days = tuple(day for day in full_days if start <= day <= end)
    first_index = next(
        (index for index, day in enumerate(full_days) if day >= start),
        len(full_days),
    )
    last_index = next(
        (index for index in range(len(full_days) - 1, -1, -1) if full_days[index] <= end),
        first_index - 1,
    )
    bar_start_index = max(0, first_index - WARMUP_MARKET_DAYS)
    bar_end_index = min(len(full_days) - 1, last_index + POST_HORIZON_MARKET_DAYS)
    if not full_days:
        return (), (), start, end
    return (
        full_days,
        event_days,
        full_days[bar_start_index],
        full_days[bar_end_index],
    )
