"""Continuous-auction limit-price checks on a witnessed raw book."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext
from functools import cached_property
from json import dumps
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator
from tivelo_contracts.base import (
    Contract,
    Digest,
    Key,
    PositiveDecimal,
    UTCDateTime,
    fingerprint,
    utc_datetime,
)
from tivelo_contracts.data import DatasetReference, Instrument
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import DefinitionReference
from tivelo_contracts.market import PriceBounds, ReferencePrice

from .calendar import CN_TIMEZONE
from .presets import compile_calendar, load_calendar_preset
from .price_presets import load_price_preset, resolve_price_rules
from .quantity_presets import load_quantity_preset, resolve_quantity_rules
from .rules import CNTradingRules

if TYPE_CHECKING:
    from tivelo_contracts.protocols import OrderBookReader

Side = Literal["buy", "sell"]
Basis = Literal["best_bid", "best_ask", "last_trade", "session_reference"]
ObservedBookAgeMs = Annotated[int, Field(strict=True, gt=0, le=86_400_000)]
_SIDE = TypeAdapter(Side)
_PRICE = TypeAdapter(PositiveDecimal)
_VERSION = "2026.1.0"
_PERCENTAGE = Decimal("0.02")


class ContinuousBook(Contract):
    """Explicit best prices and same-day last trade, valid at the witness instant.

    Null means the source confirmed absence. The caller supplies a synchronized
    snapshot or a book reconstructed through `at` from complete exchange events.
    """

    instrument_id: Key
    session_id: Key
    at: UTCDateTime
    available_at: UTCDateTime
    source_id: Key
    revision_id: Key
    price_basis: Literal["raw"]
    currency: Literal["CNY"]
    state: Literal["continuous", "halted"]
    best_bid: PositiveDecimal | None
    best_ask: PositiveDecimal | None
    last_trade: PositiveDecimal | None
    last_trade_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def known_trade(self) -> Self:
        if (self.last_trade is None) != (self.last_trade_at is None):
            raise ValueError("a last trade needs its exchange timestamp")
        if self.last_trade_at is not None and (
            self.last_trade_at > min(self.at, self.available_at)
            or self.last_trade_at.astimezone(CN_TIMEZONE).date()
            != self.at.astimezone(CN_TIMEZONE).date()
        ):
            raise ValueError("last trade must already be known on the book's trading date")
        return self


class LimitPriceCheck(Contract):
    instrument_id: Key
    at: UTCDateTime
    side: Side
    raw_limit: PositiveDecimal
    result: Literal[
        "price_valid", "invalid_limit_tick", "outside_daily_limits", "outside_price_cage"
    ]
    basis_kind: Basis
    basis_price: PositiveDecimal
    cage_boundary: PositiveDecimal
    daily_bounds: PriceBounds
    clauses: tuple[Key, Key]
    policy: DefinitionReference
    book_source: Key
    book_revision: Key
    session_reference_revision: Key | None = None
    book_event_at: UTCDateTime | None = Field(default=None, exclude_if=lambda value: value is None)
    book_available_at: UTCDateTime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class SnapshotLimitPriceCheck(Contract):
    snapshot_id: Digest
    dataset: DatasetReference
    check: LimitPriceCheck


class _Epoch(Contract):
    daily_rule_id: Key
    distance_ticks: Literal[0, 10]
    clauses: tuple[Key, Key]


class _Policy(Contract):
    source_preset: Digest
    percentage: PositiveDecimal
    epochs: tuple[_Epoch, ...]


class _Binding(Contract):
    market: DefinitionReference
    limit_prices: DefinitionReference
    observed_book_max_age_ms: ObservedBookAgeMs | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


def _load_policy(version: str):
    if version != _VERSION:
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Unknown continuous limit-price preset")
    source = load_price_preset(version=_VERSION)
    # Match the epochs and publication knowledge already pinned by the daily
    # preset. Only STAR has its own continuous-auction percentage-only clause.
    epochs = tuple(
        _Epoch(daily_rule_id=rule, distance_ticks=ticks, clauses=(clause, rounding))
        for rule, ticks, clause, rounding in (
            ("sse.main.prices.2023", 10, "sse-rules-2023:3.3.14", "sse-rules-2023:3.3.17"),
            ("sse.main.prices.2026", 10, "sse-rules-2026:3.3.14", "sse-rules-2026:3.3.17"),
            ("sse.star.prices.2023", 0, "sse-rules-2023:6.1.8", "sse-rules-2023:3.3.17"),
            ("sse.star.prices.2026", 0, "sse-rules-2026:6.8", "sse-rules-2026:3.3.17"),
            ("szse.main.prices.2023", 10, "szse-rules-2023:3.3.16", "szse-rules-2023:3.3.19"),
            ("szse.main.prices.2026", 10, "szse-rules-2026:3.3.16", "szse-rules-2026:3.3.19"),
            ("szse.chinext.prices.2023", 10, "szse-rules-2023:3.3.16", "szse-rules-2023:3.3.19"),
            ("szse.chinext.prices.2026", 10, "szse-rules-2026:3.3.16", "szse-rules-2026:3.3.19"),
        )
    )
    if {row.daily_rule_id for row in epochs} != {row.rule_id for row in source.contents.schedules}:
        raise TiveloError(
            ErrorCode.ARTIFACT_CORRUPT, "Continuous rules differ from pinned price epochs"
        )
    reference = DefinitionReference(
        definition_id="cn.equity.continuous.limit-price.v1",
        version=version,
        sha256=fingerprint(
            _Policy(source_preset=source.version_id, percentage=_PERCENTAGE, epochs=epochs)
        ),
    )
    return source, epochs, reference


def continuous_limit_reference(*, version: str) -> DefinitionReference:
    return _load_policy(version)[2]


def _basis(side: Side, book: ContinuousBook, reference: ReferencePrice | None):
    levels = (("best_ask", book.best_ask), ("best_bid", book.best_bid))
    if side == "sell":
        levels = levels[::-1]
    for name, value in (*levels, ("last_trade", book.last_trade)):
        if value is not None:
            return name, value
    if reference is None:
        raise TiveloError(
            ErrorCode.DATA_COVERAGE_INSUFFICIENT,
            "The empty book requires an exchange session reference",
        )
    return "session_reference", reference.value


def _cage(basis: Decimal, tick: Decimal, side: Side, distance_ticks: int) -> Decimal:
    direction = 1 if side == "buy" else -1
    distance = max(basis * _PERCENTAGE, distance_ticks * tick)
    rounded = ((basis + direction * distance) / tick).to_integral_value(
        rounding=ROUND_HALF_UP
    ) * tick
    if abs(rounded - basis) < tick:
        rounded = basis + direction * tick
    return max(tick, rounded)


class CNContinuousLimitOrders:
    """A price-only admission check, separate from matching and broker permissions."""

    requires_order_book = True

    def __init__(
        self,
        market: CNTradingRules,
        policy: DefinitionReference,
        *,
        observed_book_max_age_ms: ObservedBookAgeMs | None = None,
    ):
        self._preset, epochs, expected = _load_policy(policy.version)
        if policy != expected:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT,
                "Continuous limit-price preset differs from the profile pin",
            )
        calendar = market.calendar.version.contents
        expected_calendar = compile_calendar(
            load_calendar_preset(version=_VERSION),
            venue_id=calendar.venue_id,
            start=calendar.days[0].trading_date,
            end=calendar.days[-1].trading_date,
        )
        if market.calendar.version != expected_calendar:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT,
                "Continuous limit-price profile requires the pinned auction calendar",
            )
        self._market = market
        self._epochs = {row.daily_rule_id: row for row in epochs}
        self._classes = {row.spec_revision: row for row in market.configuration.instrument_classes}
        self._policy = expected
        binding = _Binding(
            market=market.reference,
            limit_prices=expected,
            observed_book_max_age_ms=observed_book_max_age_ms,
        )
        self._book_age = timedelta(milliseconds=binding.observed_book_max_age_ms or 0)
        self.reference = DefinitionReference(
            definition_id=market.reference.definition_id,
            version=market.reference.version,
            sha256=fingerprint(binding),
            parameters_json=dumps(
                {"observed_book_max_age_ms": binding.observed_book_max_age_ms}
                if self._book_age
                else {}
            ),
        )

    @property
    def execution_rules(self) -> CNTradingRules:
        return self._market

    @cached_property
    def _quantities(self):
        return load_quantity_preset(version=_VERSION)

    def continuous_session_id(self, at: datetime) -> str:
        session = self._market.calendar.session_at(at, as_of=at)
        if session is None or session.kind != "continuous":
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Event is outside continuous auction"
            )
        return session.session_id

    def cash_limit_deadline(self, instrument: Instrument, side: Side, *, at: datetime) -> datetime:
        """Check order type, quantity policy and eligibility scope before returning day close."""
        at = utc_datetime(at)
        term = self._market.trading_terms(instrument, at=at)
        classification = self._classes.get(instrument.spec_revision)
        if classification is None:
            raise TiveloError(
                ErrorCode.RULES_UNAVAILABLE, "A historical stock classification is required"
            )
        self.continuous_session_id(at)
        if side == "buy" and (
            classification.risk_status != "normal"
            or classification.listing_phase in ("delisting", "delisting_first")
            or classification.board != "main"
        ):
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "This purchase requires investor permissions and special account rules",
            )
        approved = resolve_quantity_rules(self._quantities, classification, at=at)
        limit = next((x for x in approved.order_quantities if x.order_type == "limit"), None)
        if (
            limit is None
            or term.sell_lag_sessions != 1
            or self._market.quantity_terms(instrument, "limit", at=at) != limit
        ):
            raise TiveloError(
                ErrorCode.RULES_UNAVAILABLE,
                "Configured limit quantities or settlement differ from the CN cash policy",
            )
        day = at.astimezone(CN_TIMEZONE).date()
        (trading_day,) = self._market.trading_days(day, day, as_of=at)
        return trading_day.close_at

    def check_snapshot_limit_price(
        self,
        instrument: Instrument,
        side: Side,
        raw_limit: Decimal,
        *,
        at: datetime,
        reader: OrderBookReader,
        dataset: DatasetReference,
        reference: ReferencePrice | None = None,
    ) -> SnapshotLimitPriceCheck:
        try:
            from tivelo_contracts.order_books import (
                OrderBookObservation,
                OrderBookQuery,
                select_order_book_dataset,
            )
            from tivelo_contracts.protocols import OrderBookReader
        except ModuleNotFoundError as exc:
            if exc.name != "tivelo_contracts.order_books":
                raise
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Order-book contracts are unavailable"
            ) from None
        if not isinstance(reader, OrderBookReader):
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Snapshot reader cannot query order books"
            )
        try:
            at = utc_datetime(at)
        except ValueError:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid submission time") from None
        version = select_order_book_dataset(reader.datasets, dataset)
        if dataset not in reader.manifest.contents.datasets:
            raise TiveloError(
                ErrorCode.SNAPSHOT_CORRUPT,
                "Order-book version is missing from the snapshot manifest",
            )
        if version.contents.knowledge_basis != "historical_versions":
            raise TiveloError(
                ErrorCode.FUTURE_INFORMATION,
                "Order admission requires historical order-book versions",
            )
        scope = version.contents.scope
        if scope is None:
            raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Order-book dataset scope is absent")
        if (scope.market_id, scope.asset_class) != (instrument.market_id, instrument.asset_class):
            raise TiveloError(
                ErrorCode.DATA_CONFLICT,
                "Order-book dataset belongs to another market or asset class",
            )
        if (
            instrument.instrument_id not in scope.instrument_ids
            or not scope.start <= at < scope.end
        ):
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                "Order falls outside the order-book dataset scope",
            )
        start = max(scope.start, at - self._book_age)
        records = reader.order_books(
            OrderBookQuery(
                dataset=dataset,
                instrument_ids=(instrument.instrument_id,),
                as_of=at,
                start=start,
                end=at + timedelta(microseconds=1),
            )
        )
        if not records:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                "No known order book within the submission window",
            )
        if self._book_age and len(records) > 1:
            latest = max(record.event_at for record in records)
            records = tuple(record for record in records if record.event_at == latest)
        if len(records) != 1:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Conflicting order books at the selected event time"
            )
        try:
            observed = OrderBookObservation.model_validate(records[0].model_dump())
        except ValueError:
            raise TiveloError(
                ErrorCode.SNAPSHOT_CORRUPT, "Snapshot contains an invalid order book"
            ) from None
        if (
            not start <= observed.event_at <= at
            or observed.trading_date != at.astimezone(CN_TIMEZONE).date()
            or observed.source_id not in version.contents.source_ids
            or observed.quote_currency != "CNY"
        ):
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Order book differs from its locked event or source"
            )
        book = ContinuousBook(
            at=observed.event_at,
            currency=observed.quote_currency,
            **observed.model_dump(include=set(ContinuousBook.model_fields) - {"at", "currency"}),
        )
        return SnapshotLimitPriceCheck(
            snapshot_id=reader.manifest.snapshot_id,
            dataset=dataset,
            check=self.check_limit_price(
                instrument, side, raw_limit, at=at, book=book, reference=reference
            ),
        )

    def check_limit_price(
        self,
        instrument: Instrument,
        side: Side,
        raw_limit: Decimal,
        *,
        at: datetime,
        book: ContinuousBook | None,
        reference: ReferencePrice | None = None,
    ) -> LimitPriceCheck:
        try:
            at = utc_datetime(at)
            side = _SIDE.validate_python(side)
            raw_limit = _PRICE.validate_python(raw_limit)
            if book is not None:
                book = ContinuousBook.model_validate(book.model_dump())
        except ValueError:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Invalid raw order price, submission time or order book"
            ) from None
        term = self._market.trading_terms(instrument, at=at)
        classification = self._classes.get(instrument.spec_revision)
        if classification is None:
            raise TiveloError(
                ErrorCode.RULES_UNAVAILABLE, "A historical stock classification is required"
            )
        selection = resolve_price_rules(self._preset, classification, at=at)
        epoch = self._epochs[selection.rule_id]
        if (term.price_tick, term.price_limit_mode, term.limit_up_ratio, term.limit_down_ratio) != (
            selection.price_tick,
            selection.price_limit_mode,
            selection.limit_up_ratio,
            selection.limit_down_ratio,
        ):
            raise TiveloError(
                ErrorCode.RULES_UNAVAILABLE, "Trading terms differ from the effective price preset"
            )
        session = self._market.calendar.session_at(at, as_of=at)
        if session is None or session.kind != "continuous":
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Limit-price check requires a continuous auction"
            )
        if book is None:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT, "No order book at the submission instant"
            )
        if book.instrument_id != instrument.instrument_id or book.session_id != session.session_id:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Order book belongs to another instrument or session"
            )
        if book.at > at or book.available_at > at:
            raise TiveloError(
                ErrorCode.FUTURE_INFORMATION, "Order book was not known at submission"
            )
        if self._book_age and (book.at < session.start or book.available_at < book.at):
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Order book timing differs from the submission session"
            )
        if at - book.at > self._book_age:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Order book is stale at submission"
            )
        if book.state != "continuous":
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "The security is temporarily halted"
            )
        if (
            book.best_bid is not None
            and book.best_ask is not None
            and book.best_bid >= book.best_ask
        ):
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "A continuous order book must be uncrossed"
            )
        if reference is not None:
            if (
                reference.instrument_id != instrument.instrument_id
                or reference.trading_date != at.astimezone(CN_TIMEZONE).date()
            ):
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID,
                    "Exchange reference belongs to another instrument/date",
                )
            if reference.available_at > at:
                raise TiveloError(
                    ErrorCode.FUTURE_INFORMATION, "Exchange reference was not known at submission"
                )
        daily = self._market.price_bounds(instrument, reference, at=at)
        kind, basis = _basis(side, book, reference)
        with localcontext(Context(prec=96)):
            tick = term.price_tick
            prices = (book.best_bid, book.best_ask, book.last_trade, basis)
            if any(price is not None and price % tick for price in prices):
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Order book price violates the market tick"
                )
            outside_daily = False
            if daily.state == "limited":
                assert daily.lower is not None and daily.upper is not None
                if any(
                    price is not None and not daily.lower <= price <= daily.upper
                    for price in prices
                ):
                    raise TiveloError(
                        ErrorCode.DATA_CONFLICT, "Order book price lies outside the daily limits"
                    )
                outside_daily = not daily.lower <= raw_limit <= daily.upper
            cage = _cage(basis, tick, side, epoch.distance_ticks)
            if raw_limit % tick:
                result = "invalid_limit_tick"
            elif outside_daily:
                result = "outside_daily_limits"
            elif (side == "buy" and raw_limit > cage) or (side == "sell" and raw_limit < cage):
                result = "outside_price_cage"
            else:
                result = "price_valid"
        return LimitPriceCheck(
            instrument_id=instrument.instrument_id,
            at=at,
            side=side,
            raw_limit=raw_limit,
            result=result,
            basis_kind=kind,
            basis_price=basis,
            cage_boundary=cage,
            daily_bounds=daily,
            clauses=epoch.clauses,
            policy=self._policy,
            book_source=book.source_id,
            book_revision=book.revision_id,
            book_event_at=book.at if self._book_age else None,
            book_available_at=book.available_at if self._book_age else None,
            session_reference_revision=reference.revision_id
            if kind == "session_reference"
            else None,
        )
