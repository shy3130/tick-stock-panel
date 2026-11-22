"""Public structural protocols. Backtest consumers need no data-engine import."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from .data import (
    AdjustmentFactor,
    Bar,
    BarQuery,
    CorporateAction,
    DatasetVersion,
    EventQuery,
    HistoryQuery,
    Instrument,
    OpeningObservation,
    ProjectedBar,
    SnapshotManifest,
)
from .execution import (
    DefinitionReference,
    ExtensionPin,
    PriceValue,
    PriceViewSpec,
    RunLock,
    SignalBatch,
    StrategyContext,
)
from .extension_fields import ExtensionQuery, ExtensionValue
from .market import (
    FeeQuote,
    OrderQuantityTerms,
    OrderType,
    PriceBounds,
    ReferencePrice,
    TradingDay,
    TradingTerms,
)
from .order_books import OrderBookObservation, OrderBookQuery
from .trade_prints import TradePrint, TradePrintQuery


@runtime_checkable
class DataReader(Protocol):
    @property
    def manifest(self) -> SnapshotManifest: ...

    @property
    def datasets(self) -> tuple[DatasetVersion, ...]: ...

    def bars(self, query: BarQuery) -> tuple[Bar, ...]: ...

    def instruments(self, query: HistoryQuery) -> tuple[Instrument, ...]: ...

    def actions(self, query: HistoryQuery) -> tuple[CorporateAction, ...]: ...

    def factors(self, query: HistoryQuery) -> tuple[AdjustmentFactor, ...]: ...


@runtime_checkable
class ExecutionDataReader(DataReader, Protocol):
    def openings(self, query: EventQuery) -> tuple[OpeningObservation, ...]: ...


@runtime_checkable
class ExtensionFieldReader(Protocol):
    """Optional. Existing read-only market adapters need no extension support."""

    def extension_values(self, query: ExtensionQuery) -> tuple[ExtensionValue, ...]: ...


@runtime_checkable
class OrderBookReader(Protocol):
    """Optional read-only order-book support."""

    @property
    def manifest(self) -> SnapshotManifest: ...

    @property
    def datasets(self) -> tuple[DatasetVersion, ...]: ...

    def order_books(self, query: OrderBookQuery) -> tuple[OrderBookObservation, ...]: ...


@runtime_checkable
class TradePrintReader(Protocol):
    def trade_prints(self, query: TradePrintQuery) -> tuple[TradePrint, ...]: ...


class ArrowBatch(Protocol):
    """Arrow C Data interface; the contracts package does not import PyArrow."""

    def __arrow_c_array__(self, requested_schema: object = None) -> tuple[object, object]: ...


@runtime_checkable
class ColumnarDataReader(DataReader, Protocol):
    """Optional bulk path; consumers may still use a plain in-memory DataReader."""

    def iter_bar_batches(
        self, query: BarQuery, *, batch_size: int = 65536
    ) -> Iterator[ArrowBatch]: ...


@runtime_checkable
class Strategy(Protocol):
    def decide(self, context: StrategyContext) -> SignalBatch: ...


@runtime_checkable
class Observer(Protocol):
    def cancelled(self) -> bool: ...

    def progress(self, completed: int, total: int, stage: str) -> None: ...


@runtime_checkable
class PriceViewProvider(Protocol):
    provider_id: str

    def project(
        self,
        bars: tuple[Bar, ...],
        factors: tuple[AdjustmentFactor, ...],
        spec: PriceViewSpec,
        *,
        decision_at: datetime,
    ) -> tuple[Bar | ProjectedBar, ...]: ...

    def to_raw(self, value: PriceValue, *, decision_at: datetime) -> Decimal: ...


class PriceViewRuntime(Protocol):
    """A host's verified release registry and per-run provider admission."""

    def resolve(self, provider_id: str) -> ExtensionPin: ...

    def open(self, lock: RunLock) -> PriceViewProvider: ...


@runtime_checkable
class MarketRules(Protocol):
    market_id: str
    version: str

    def supports(self, instrument: Instrument) -> bool: ...

    def buy_quantity(
        self, instrument: Instrument, quantity: Decimal, *, at: datetime
    ) -> Decimal: ...

    def sellable_at(self, instrument: Instrument, filled_at: datetime) -> datetime: ...

    def fees(
        self,
        instrument: Instrument,
        side: str,
        quantity: Decimal,
        raw_price: Decimal,
        *,
        at: datetime,
    ) -> Decimal: ...


@runtime_checkable
class ExecutionRules(MarketRules, Protocol):
    @property
    def reference(self) -> DefinitionReference: ...

    def trading_terms(self, instrument: Instrument, *, at: datetime) -> TradingTerms: ...

    def is_open(self, instrument: Instrument, *, at: datetime) -> bool: ...

    def sell_quantity(
        self, instrument: Instrument, quantity: Decimal, *, available: Decimal, at: datetime
    ) -> Decimal: ...

    def price_bounds(
        self, instrument: Instrument, reference: ReferencePrice | None, *, at: datetime
    ) -> PriceBounds: ...

    def fee_breakdown(
        self,
        instrument: Instrument,
        side: str,
        quantity: Decimal,
        raw_price: Decimal,
        *,
        at: datetime,
    ) -> FeeQuote: ...


@runtime_checkable
class OrderExecutionRules(Protocol):
    """Optional explicit-order extension; never assume a legacy provider accepts it."""

    def quantity_terms(
        self, instrument: Instrument, order_type: OrderType, *, at: datetime
    ) -> OrderQuantityTerms: ...

    def order_quantity(
        self,
        instrument: Instrument,
        side: str,
        quantity: Decimal,
        *,
        order_type: OrderType,
        available: Decimal | None = None,
        at: datetime,
    ) -> Decimal: ...


@runtime_checkable
class DailyExecutionRules(ExecutionRules, Protocol):
    # Required by the reference runner's fee-reserving quantity search.
    buy_cost_monotone: bool

    def trading_days(
        self, start: date, end: date, *, as_of: datetime
    ) -> tuple[TradingDay, ...]: ...

    def next_open(self, after_date: date, *, as_of: datetime) -> datetime: ...
