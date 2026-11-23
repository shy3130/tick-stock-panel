"""Small in-memory reference reader for third-party contract tests.

This module intentionally does no file I/O, synchronization or provider routing.
Production readers can run the same conformance tests against their own storage.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import TypeVar

from .base import Contract, canonical_bytes, fingerprint, utc_datetime
from .data import (
    AdjustmentFactor,
    Artifact,
    Bar,
    BarQuery,
    CorporateAction,
    Coverage,
    CoverageIssue,
    DatasetContents,
    DatasetReference,
    DatasetScope,
    DatasetVersion,
    EventQuery,
    HistoryQuery,
    Instrument,
    KnowledgeBasis,
    OpeningObservation,
    SnapshotContents,
    SnapshotManifest,
    SymbolMapping,
)
from .errors import ErrorCode, TiveloError
from .extension_fields import ExtensionQuery, ExtensionValue, resolve_field
from .order_books import OrderBookObservation, OrderBookQuery, select_order_book_dataset
from .trade_prints import TradePrint, TradePrintQuery, select_trade_dataset

Record = TypeVar("Record", bound=Contract)


def _latest(records: Iterable[Record], key: Callable[[Record], tuple]) -> tuple[Record, ...]:
    selected: dict[tuple, list[Record]] = {}
    for record in records:
        identity = key(record)
        previous = selected.get(identity, [])
        if not previous or record.available_at > previous[0].available_at:
            selected[identity] = [record]
        elif record.available_at == previous[0].available_at:
            previous.append(record)
    for revisions in selected.values():
        if any(r != revisions[0] for r in revisions[1:]):
            raise TiveloError(ErrorCode.DATA_CONFLICT, "ambiguous visible revisions")
    return tuple(selected[identity][0] for identity in sorted(selected))


class InMemoryReader:
    def __init__(
        self,
        *,
        bars: Iterable[Bar] = (),
        instruments: Iterable[Instrument] = (),
        actions: Iterable[CorporateAction] = (),
        factors: Iterable[AdjustmentFactor] = (),
        symbols: Iterable[SymbolMapping] = (),
        openings: Iterable[OpeningObservation] = (),
        order_books: Iterable[OrderBookObservation] = (),
        trade_prints: Iterable[TradePrint] = (),
        extension_values: Mapping[str, Iterable[ExtensionValue]] | None = None,
        knowledge_basis: KnowledgeBasis,
        scopes: Mapping[str, DatasetScope] | None = None,
    ) -> None:
        self._bars = tuple(bars)
        self._instruments = tuple(instruments)
        self._actions = tuple(actions)
        self._factors = tuple(factors)
        self._symbols = tuple(symbols)
        self._openings = tuple(openings)
        self._order_books = tuple(order_books)
        self._trade_prints = tuple(trade_prints)
        self._extensions = {key: tuple(rows) for key, rows in (extension_values or {}).items()}
        scopes = dict(scopes or {})
        for kind, records in (
            ("order_book_observation", self._order_books),
            ("trade_print", self._trade_prints),
        ):
            if not records:
                continue
            scope = scopes.get(kind)
            if scope is None or any(
                row.instrument_id not in scope.instrument_ids
                or not scope.start <= row.event_at < scope.end
                for row in records
            ):
                raise ValueError("in-memory book and trade events require a matching capture scope")
            if knowledge_basis != "historical_versions" and any(
                row.available_at != row.ingested_at for row in records
            ):
                raise ValueError(
                    "reconstructed book and trade events may only be known at ingestion"
                )
        datasets = []
        groups = [
            ("instrument", self._instruments, None),
            ("corporate_action", self._actions, None),
            ("adjustment_factor", self._factors, None),
            ("symbol_mapping", self._symbols, None),
            ("opening_observation", self._openings, None),
            ("order_book_observation", self._order_books, None),
            ("trade_print", self._trade_prints, None),
        ]
        groups.extend(
            ("bar", tuple(b for b in self._bars if b.price_basis == basis), basis)
            for basis in ("raw", "qfq", "hfq")
        )
        named = [
            (kind + (f".{basis}" if basis else ""), kind, records, basis)
            for kind, records, basis in groups
        ]
        named.extend(
            (key, "extension_value", records, None) for key, records in self._extensions.items()
        )
        for dataset_id, record_type, records, basis in named:
            if not records and dataset_id not in scopes and record_type != "extension_value":
                continue
            if record_type == "extension_value":
                scope = scopes.get(dataset_id)
                if scope is None or scope.extension_field is None:
                    raise ValueError("in-memory extension fields require a versioned scope")
                field = scope.extension_field
                digest = fingerprint(field)
                universe = set(scope.instrument_ids) or {None}
                if any(
                    not row.matches(field, digest)
                    or row.instrument_id not in universe
                    or not scope.start <= row.effective_at < scope.end
                    for row in records
                ):
                    raise ValueError(
                        "in-memory observation differs from its declared field or scope"
                    )
                missing = universe - {row.instrument_id for row in records}
                coverage = Coverage(
                    status="empty" if not records else "partial" if missing else "complete",
                    requested_units=len(universe),
                    completed_units=len(universe) - len(missing),
                    row_count=len(records),
                    issues=tuple(
                        CoverageIssue(
                            instrument_id=subject,
                            reason="missing",
                            detail="No extension observation for this subject",
                        )
                        for subject in sorted(missing)
                    ),
                )
            else:
                coverage = Coverage(
                    status="complete" if records else "empty",
                    requested_units=1,
                    completed_units=1,
                    row_count=len(records),
                    issues=()
                    if records
                    else (
                        CoverageIssue(
                            reason="no_events", detail="Explicit synthetic empty fixture coverage"
                        ),
                    ),
                )
            content = b"\n".join(sorted(canonical_bytes(r) for r in records)) + b"\n"
            datasets.append(
                DatasetVersion.create(
                    DatasetContents(
                        dataset_id=dataset_id,
                        record_type=record_type,
                        knowledge_basis=knowledge_basis,
                        price_basis=basis,
                        source_ids=tuple(sorted({r.source_id for r in records}))
                        or ("memory-fixture",),
                        artifacts=(
                            Artifact(
                                path=f"{dataset_id}.jsonl",
                                sha256=hashlib.sha256(content).hexdigest(),
                                size_bytes=len(content),
                                row_count=len(records),
                            ),
                        )
                        if records
                        else (),
                        coverage=coverage,
                        scope=scopes.get(dataset_id),
                        normalization_version="memory-reference-v1",
                        source_selection_digest=hashlib.sha256(
                            b"explicit-fixture-input-v1"
                        ).hexdigest(),
                    )
                )
            )
        self._datasets = tuple(sorted(datasets, key=lambda d: d.contents.dataset_id))
        self._manifest = SnapshotManifest.create(
            SnapshotContents(
                datasets=tuple(
                    DatasetReference(dataset_id=d.contents.dataset_id, version_id=d.version_id)
                    for d in self._datasets
                )
            )
        )

    @property
    def manifest(self) -> SnapshotManifest:
        return self._manifest

    @property
    def datasets(self) -> tuple[DatasetVersion, ...]:
        return self._datasets

    def extension_values(self, query: ExtensionQuery) -> tuple[ExtensionValue, ...]:
        resolve_field(self.datasets, query, query.instrument_ids)
        subjects = set(query.instrument_ids) or {None}
        revisions = _latest(
            (
                row
                for row in self._extensions[query.dataset_id]
                if row.instrument_id in subjects
                and row.available_at <= query.as_of
                and row.effective_at <= query.effective_at
            ),
            lambda row: (row.instrument_id, row.effective_at),
        )
        current = {row.instrument_id: row for row in revisions}
        return tuple(
            row
            for row in current.values()
            if row.valid_until is None or query.effective_at < row.valid_until
        )

    def bars(self, query: BarQuery) -> tuple[Bar, ...]:
        ids = set(query.instrument_ids)
        records = (
            b
            for b in self._bars
            if b.instrument_id in ids
            and b.timeframe == query.timeframe
            and query.start <= b.bar_start < query.end
            and b.price_basis == query.price_basis
            and b.available_at <= query.as_of
        )
        return tuple(
            b
            for b in _latest(records, lambda b: (b.bar_start, b.instrument_id, b.timeframe))
            if b.bar_end <= query.as_of and b.is_final
        )

    def openings(self, query: EventQuery) -> tuple[OpeningObservation, ...]:
        records = (
            o
            for o in self._openings
            if o.instrument_id in query.instrument_ids
            and query.start <= o.event_at < query.end
            and o.available_at <= query.as_of
        )
        return tuple(
            o
            for o in _latest(records, lambda o: (o.instrument_id, o.event_at))
            if o.is_final and o.event_at <= query.as_of
        )

    def order_books(self, query: OrderBookQuery) -> tuple[OrderBookObservation, ...]:
        select_order_book_dataset(self.datasets, query.dataset)
        return _latest(
            (
                row
                for row in self._order_books
                if row.instrument_id in query.instrument_ids
                and query.start <= row.event_at < query.end
                and row.available_at <= query.as_of
            ),
            lambda row: (row.instrument_id, row.event_at),
        )

    def trade_prints(self, query: TradePrintQuery) -> tuple[TradePrint, ...]:
        select_trade_dataset(self.datasets, query.dataset)
        latest = _latest(
            (
                row
                for row in self._trade_prints
                if row.instrument_id in query.instrument_ids
                and row.trading_date == query.trading_date
                and (query.sequence is None or row.sequence == query.sequence)
                and row.available_at <= query.as_of
            ),
            lambda row: (row.instrument_id, row.trading_date, row.sequence),
        )
        return tuple(row for row in latest if query.start <= row.event_at < query.end)

    def instruments(self, query: HistoryQuery) -> tuple[Instrument, ...]:
        versions = _latest(
            (
                i
                for i in self._instruments
                if i.instrument_id in query.instrument_ids and i.available_at <= query.as_of
            ),
            lambda i: (i.instrument_id, i.valid_from),
        )
        active = tuple(
            i
            for i in versions
            if i.valid_from <= query.effective_at
            and (i.valid_to is None or query.effective_at < i.valid_to)
        )
        if len({i.instrument_id for i in active}) != len(active):
            raise TiveloError(ErrorCode.DATA_CONFLICT, "overlapping instrument validity intervals")
        return active

    def actions(self, query: HistoryQuery) -> tuple[CorporateAction, ...]:
        versions = _latest(
            (
                a
                for a in self._actions
                if a.instrument_id in query.instrument_ids and a.available_at <= query.as_of
            ),
            lambda a: (a.instrument_id, a.action_id),
        )
        return tuple(
            a for a in versions if a.effective_at <= query.effective_at and not a.cancelled
        )

    def factors(self, query: HistoryQuery) -> tuple[AdjustmentFactor, ...]:
        versions = _latest(
            (
                f
                for f in self._factors
                if f.instrument_id in query.instrument_ids and f.available_at <= query.as_of
            ),
            lambda f: (f.instrument_id, f.definition_id, f.baseline_id, f.factor_id),
        )
        return tuple(
            f for f in versions if f.effective_at <= query.effective_at and not f.cancelled
        )

    def resolve_symbol(
        self,
        symbol: str,
        *,
        source_id: str,
        venue_id: str,
        effective_at: datetime,
        as_of: datetime,
    ) -> str:
        effective_at, as_of = utc_datetime(effective_at), utc_datetime(as_of)
        versions = _latest(
            (s for s in self._symbols if s.source_id == source_id and s.available_at <= as_of),
            lambda s: (s.instrument_id, s.valid_from),
        )
        matches = tuple(
            s
            for s in versions
            if s.symbol == symbol
            and s.venue_id == venue_id
            and s.valid_from <= effective_at
            and (s.valid_to is None or effective_at < s.valid_to)
        )
        if not matches:
            raise TiveloError(ErrorCode.INSTRUMENT_NOT_FOUND, "no visible symbol mapping")
        if len(matches) != 1:
            raise TiveloError(ErrorCode.AMBIGUOUS_INSTRUMENT, "symbol maps to multiple instruments")
        return matches[0].instrument_id
