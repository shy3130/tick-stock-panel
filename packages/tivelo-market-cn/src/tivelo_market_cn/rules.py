"""Configured CN cash-equity rules; historical metadata is always explicit.

No broker rates or security classifications are inferred. A configuration is a
locked collection of resolved historical rules, not a mutable global registry.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Context,
    Decimal,
    localcontext,
)
from typing import Literal, Self, TypeVar

from pydantic import Field, TypeAdapter, model_validator
from tivelo_contracts.base import (
    Contract,
    Key,
    NonNegativeDecimal,
    PositiveDecimal,
    fingerprint,
    utc_datetime,
)
from tivelo_contracts.data import HistoricalRecord, Instrument
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import DefinitionReference
from tivelo_contracts.market import (
    FeeComponent,
    FeeQuote,
    OrderQuantityTerms,
    OrderType,
    PriceBounds,
    ReferencePrice,
    TradingDay,
    TradingTerms,
)

from .calendar import CN_TIMEZONE, CNCalendar
from .classification import CNEquityClass

_NONNEGATIVE = TypeAdapter(NonNegativeDecimal)
_POSITIVE = TypeAdapter(PositiveDecimal)
_ORDER_TYPE = TypeAdapter(OrderType)
_ROUNDING = {
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
    "up": ROUND_CEILING,
    "down": ROUND_DOWN,
}
_Record = TypeVar("_Record", bound=HistoricalRecord)


def _validate_versions(versions: list[HistoricalRecord]) -> None:
    if len({(item.valid_from, item.available_at) for item in versions}) != len(versions):
        raise ValueError("rule versions cannot have conflicting effective/knowledge times")
    if len({item.revision_id for item in versions}) != len(versions):
        raise ValueError("a rule revision identifies one immutable record within its history")


def _known_version(versions: tuple[_Record, ...], at: datetime) -> _Record | None:
    # Versions are indexed by effective start then knowledge time. Expiration
    # is checked after selection, so an ended correction cannot revive its past.
    selected = next(
        (item for item in reversed(versions) if item.valid_from <= at and item.available_at <= at),
        None,
    )
    if selected is not None and (selected.valid_to is None or at < selected.valid_to):
        return selected
    return None


class FeeRule(HistoricalRecord):
    fee_id: Key
    side: Literal["buy", "sell", "both"]
    rate: NonNegativeDecimal
    minimum: NonNegativeDecimal
    quantum: PositiveDecimal
    rounding: Literal["half_up", "half_even", "up", "down"]
    included_fee_ids: tuple[Key, ...] = ()
    source_reference: Key

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (
            len(set(self.included_fee_ids)) != len(self.included_fee_ids)
            or self.fee_id in self.included_fee_ids
        ):
            raise ValueError("included fees must be unique and cannot include self")
        with localcontext(Context(prec=96)):
            if self.minimum % self.quantum:
                raise ValueError("minimum fee must align with its charge quantum")
        return self


class RuleConfiguration(Contract):
    schema_version: Literal["1.0", "1.1", "1.2", "1.3"] = "1.0"
    profile_id: Key
    profile_version: Key
    currency: Literal["CNY"]
    terms: tuple[TradingTerms, ...] = Field(min_length=1)
    required_fee_ids: tuple[Key, ...] = Field(min_length=1)
    fee_rules: tuple[FeeRule, ...]
    instrument_classes: tuple[CNEquityClass, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.schema_version != "1.3" and any(
            item.odd_lot_policy == "remainder_once"
            for term in self.terms
            for item in (term, *term.order_quantity_rules)
        ):
            raise ValueError("remainder_once requires rule configuration schema 1.3")
        if self.schema_version == "1.0" and any(term.order_quantity_rules for term in self.terms):
            raise ValueError("order-specific quantities require rule configuration schema 1.1")
        if self.schema_version not in ("1.2", "1.3"):
            if len({term.spec_revision for term in self.terms}) != len(self.terms):
                raise ValueError("historical term versions require configuration schema 1.2")
            if self.instrument_classes:
                raise ValueError("explicit instrument classes require configuration schema 1.2")
        term_groups: dict[str, list[TradingTerms]] = {}
        for term in self.terms:
            term_groups.setdefault(term.spec_revision, []).append(term)
        identity_fields = (
            "market_id",
            "venue_id",
            "asset_class",
            "instrument_type",
            "quantity_unit",
            "currency",
        )
        for versions in term_groups.values():
            _validate_versions(versions)
            if len({tuple(getattr(t, key) for key in identity_fields) for t in versions}) != 1:
                raise ValueError("a specification cannot change its instrument identity")
        classes = {item.spec_revision: item for item in self.instrument_classes}
        if len(classes) != len(self.instrument_classes):
            raise ValueError("instrument classes must be unique")
        if classes:
            if classes.keys() != term_groups.keys():
                raise ValueError("declared instrument classes and configured specifications differ")
            for key, classification in classes.items():
                term = term_groups[key][0]
                if (
                    term.venue_id != classification.venue_id
                    or term.instrument_type != classification.instrument_type
                ):
                    raise ValueError("instrument class does not match its configured terms")
        if tuple(sorted(set(self.required_fee_ids))) != self.required_fee_ids:
            raise ValueError("required fee IDs must be unique and sorted")
        grouped: dict[str, list[FeeRule]] = {}
        for fee in self.fee_rules:
            if fee.fee_id not in self.required_fee_ids:
                raise ValueError("every configured fee must be declared required")
            grouped.setdefault(fee.fee_id, []).append(fee)
        for versions in grouped.values():
            if self.schema_version in ("1.2", "1.3"):
                _validate_versions(versions)
                continue
            ordered = sorted(versions, key=lambda item: item.valid_from)
            for previous, following in zip(ordered, ordered[1:], strict=False):
                if previous.valid_to is None or previous.valid_to > following.valid_from:
                    raise ValueError("fee effective periods cannot overlap")
        return self


class _Binding(Contract):
    configuration: RuleConfiguration
    calendar_version: Key


class CNTradingRules:
    market_id = "cn.equity"
    buy_cost_monotone = True  # Nonnegative linear rates, minima and monotone rounding.

    def __init__(self, configuration: RuleConfiguration, calendar: CNCalendar):
        self._configuration = RuleConfiguration.model_validate_json(configuration.model_dump_json())
        self._calendar = calendar
        grouped: dict[str, list[TradingTerms]] = {}
        for term in self.configuration.terms:
            grouped.setdefault(term.spec_revision, []).append(term)
            if (
                term.market_id != self.market_id
                or term.venue_id != calendar.version.contents.venue_id
                or term.currency != configuration.currency
                or term.asset_class != "equity"
                or term.quantity_unit != "share"
            ):
                raise TiveloError(
                    ErrorCode.UNSUPPORTED_ASSET, "CN cash-equity profile has incompatible terms"
                )
            quantities = tuple(
                getattr(specification, name)
                for specification in (term, *term.order_quantity_rules)
                for name in (
                    "buy_minimum",
                    "buy_increment",
                    "buy_maximum",
                    "sell_minimum",
                    "sell_increment",
                    "sell_maximum",
                )
            )
            if any(value != value.to_integral_value() for value in quantities):
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "CN cash-equity quantities must be whole shares"
                )
        self._terms = {
            key: tuple(sorted(versions, key=lambda term: (term.valid_from, term.available_at)))
            for key, versions in grouped.items()
        }
        fee_groups: dict[str, list[FeeRule]] = {}
        for fee in self.configuration.fee_rules:
            fee_groups.setdefault(fee.fee_id, []).append(fee)
        self._fees = {
            key: tuple(sorted(versions, key=lambda fee: (fee.valid_from, fee.available_at)))
            for key, versions in fee_groups.items()
        }
        self._reference = DefinitionReference(
            definition_id=configuration.profile_id,
            version=configuration.profile_version,
            sha256=fingerprint(
                _Binding(
                    configuration=self.configuration, calendar_version=calendar.version.version_id
                )
            ),
        )

    @property
    def configuration(self) -> RuleConfiguration:
        return self._configuration

    @property
    def calendar(self) -> CNCalendar:
        return self._calendar

    @property
    def reference(self) -> DefinitionReference:
        return self._reference

    @property
    def version(self) -> str:
        return self.configuration.profile_version

    def trading_days(self, start: date, end: date, *, as_of: datetime) -> tuple[TradingDay, ...]:
        return self.calendar.trading_days(start, end, as_of=as_of)

    def next_open(self, after_date: date, *, as_of: datetime) -> datetime:
        return self.calendar.next_trading_open(after_date, as_of=as_of)

    def supports(self, instrument: Instrument) -> bool:
        versions = self._terms.get(instrument.spec_revision)
        term = versions[0] if versions else None
        return bool(
            term is not None
            and instrument.market_id == self.market_id
            and instrument.asset_class == "equity"
            and instrument.venue_id == term.venue_id
            and instrument.instrument_type == term.instrument_type
            and instrument.calendar_id == self.calendar.version.contents.calendar_id
            and instrument.timezone == CN_TIMEZONE.key
            and instrument.quote_currency == instrument.settlement_currency == term.currency
        )

    def trading_terms(self, instrument: Instrument, *, at: datetime) -> TradingTerms:
        at = utc_datetime(at)
        if not self.supports(instrument):
            raise TiveloError(
                ErrorCode.UNSUPPORTED_ASSET, "Instrument has no matching CN cash-equity terms"
            )
        if (
            instrument.available_at > at
            or instrument.valid_from > at
            or (instrument.valid_to is not None and at >= instrument.valid_to)
        ):
            raise TiveloError(
                ErrorCode.FUTURE_INFORMATION, "Instrument state is not known/effective at execution"
            )
        if not instrument.tradable:
            raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Instrument is not tradable")
        term = _known_version(self._terms[instrument.spec_revision], at)
        if term is None:
            raise TiveloError(
                ErrorCode.RULES_UNAVAILABLE, "Trading terms are not known/effective at execution"
            )
        return term

    @staticmethod
    def _floor(
        quantity: Decimal, minimum: Decimal, increment: Decimal, maximum: Decimal
    ) -> Decimal:
        with localcontext(Context(prec=96)):
            quantity = min(quantity, maximum)
            if quantity < minimum:
                return Decimal(0)
            return (
                minimum
                + ((quantity - minimum) / increment).to_integral_value(rounding=ROUND_DOWN)
                * increment
            )

    def quantity_terms(
        self, instrument: Instrument, order_type: OrderType, *, at: datetime
    ) -> OrderQuantityTerms:
        order_type = _ORDER_TYPE.validate_python(order_type)
        term = self.trading_terms(instrument, at=at)
        if term.order_quantity_rules:
            for rule in term.order_quantity_rules:
                if rule.order_type == order_type:
                    return rule
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Order type is not allowed by these trading terms"
            )
        return OrderQuantityTerms(
            order_type=order_type,
            **{
                name: getattr(term, name)
                for name in OrderQuantityTerms.model_fields
                if name != "order_type"
            },
        )

    def _quantity_terms(
        self, instrument: Instrument, order_type: OrderType | None, *, at: datetime
    ) -> TradingTerms | OrderQuantityTerms:
        if order_type is not None:
            return self.quantity_terms(instrument, order_type, at=at)
        term = self.trading_terms(instrument, at=at)
        if term.order_quantity_rules:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID,
                "An explicit order type is required by these trading terms",
            )
        return term

    def order_quantity(
        self,
        instrument: Instrument,
        side: str,
        quantity: Decimal,
        *,
        order_type: OrderType,
        available: Decimal | None = None,
        at: datetime,
    ) -> Decimal:
        if side == "buy":
            return self.buy_quantity(instrument, quantity, at=at, order_type=order_type)
        if side == "sell" and available is not None:
            return self.sell_quantity(
                instrument, quantity, available=available, at=at, order_type=order_type
            )
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Sell sizing requires available quantity")

    def buy_quantity(
        self,
        instrument: Instrument,
        quantity: Decimal,
        *,
        at: datetime,
        order_type: OrderType | None = None,
    ) -> Decimal:
        term = self._quantity_terms(instrument, order_type, at=at)
        return self._floor(
            _NONNEGATIVE.validate_python(quantity),
            term.buy_minimum,
            term.buy_increment,
            term.buy_maximum,
        )

    def is_open(self, instrument: Instrument, *, at: datetime) -> bool:
        at = utc_datetime(at)
        self.trading_terms(instrument, at=at)
        return self.calendar.session_at(at, as_of=at) is not None

    def sell_quantity(
        self,
        instrument: Instrument,
        quantity: Decimal,
        *,
        available: Decimal,
        at: datetime,
        order_type: OrderType | None = None,
    ) -> Decimal:
        term = self._quantity_terms(instrument, order_type, at=at)
        quantity, available = (
            _NONNEGATIVE.validate_python(quantity),
            _NONNEGATIVE.validate_python(available),
        )
        if term.odd_lot_policy == "remainder_once":
            with localcontext(Context(prec=96)):
                if available % 1:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID,
                        "CN odd-lot selling requires whole-share available holdings",
                    )
                ceiling = min(quantity, available, term.sell_maximum)
                whole = self._floor(
                    ceiling, term.sell_minimum, term.sell_increment, term.sell_maximum
                )
                remainder = available % term.sell_increment
                if remainder and ceiling >= remainder:
                    with_remainder = (
                        remainder
                        + ((ceiling - remainder) // term.sell_increment) * term.sell_increment
                    )
                    return max(whole, with_remainder)
                return whole
        if (
            term.odd_lot_policy == "all_remaining"
            and quantity >= available
            and available <= term.sell_maximum
        ):
            return available
        return self._floor(
            min(quantity, available), term.sell_minimum, term.sell_increment, term.sell_maximum
        )

    def sellable_at(self, instrument: Instrument, filled_at: datetime) -> datetime:
        filled_at = utc_datetime(filled_at)
        term = self.trading_terms(instrument, at=filled_at)
        if self.calendar.session_at(filled_at, as_of=filled_at) is None:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Fill is outside a declared trading session"
            )
        sellable = filled_at
        for _ in range(term.sell_lag_sessions):
            try:
                sellable = self.calendar.next_trading_open(
                    sellable.astimezone(CN_TIMEZONE).date(), as_of=filled_at
                )
            except TiveloError as exc:
                if not self.calendar.from_bars or exc.code != ErrorCode.DATA_COVERAGE_INSUFFICIENT:
                    raise
                # No later opening in the run, so the lot remains locked throughout it.
                return datetime.max.replace(tzinfo=UTC)
        return sellable

    def price_bounds(
        self, instrument: Instrument, reference: ReferencePrice | None, *, at: datetime
    ) -> PriceBounds:
        at = utc_datetime(at)
        term = self.trading_terms(instrument, at=at)
        if term.price_limit_mode == "unknown":
            raise TiveloError(ErrorCode.RULES_UNAVAILABLE, "Price-limit status is unknown")
        if term.price_limit_mode == "unlimited":
            return PriceBounds(state="unlimited", rule_revision=term.revision_id)
        if reference is None:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                "Explicit exchange reference price is required",
            )
        if (
            reference.instrument_id != instrument.instrument_id
            or reference.trading_date != at.astimezone(CN_TIMEZONE).date()
        ):
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Price reference belongs to another instrument/date"
            )
        if reference.available_at > at:
            raise TiveloError(
                ErrorCode.FUTURE_INFORMATION, "Exchange reference was not yet available"
            )
        assert term.limit_up_ratio is not None and term.limit_down_ratio is not None
        with localcontext(Context(prec=96)):

            def rounded(value, rounding=ROUND_HALF_UP):
                return (value / term.price_tick).to_integral_value(rounding=rounding) * term.price_tick

            ceiling = reference.value * (1 + term.limit_up_ratio)
            floor = reference.value * (1 - term.limit_down_ratio)
            if term.venue_id == "XBSE":
                # The quoted price must be on the tick grid and within the published range.
                upper = rounded(ceiling, ROUND_FLOOR)
                lower = rounded(floor, ROUND_CEILING)
            else:
                upper, lower = rounded(ceiling), rounded(floor)
                if upper - reference.value < term.price_tick:
                    upper = rounded(reference.value + term.price_tick)
                if reference.value - lower < term.price_tick:
                    lower = rounded(reference.value - term.price_tick)
            lower = max(lower, term.price_tick)
        return PriceBounds(
            state="limited",
            lower=lower,
            upper=upper,
            reference_revision=reference.revision_id,
            rule_revision=term.revision_id,
        )

    def fee_breakdown(
        self,
        instrument: Instrument,
        side: str,
        quantity: Decimal,
        raw_price: Decimal,
        *,
        at: datetime,
    ) -> FeeQuote:
        at = utc_datetime(at)
        self.trading_terms(instrument, at=at)
        if side not in ("buy", "sell"):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Unsupported fee side")
        quantity, raw_price = (
            _POSITIVE.validate_python(quantity),
            _POSITIVE.validate_python(raw_price),
        )
        selected = {
            key: rule
            for key, versions in self._fees.items()
            if (rule := _known_version(versions, at)) is not None
        }
        if set(selected) != set(self.configuration.required_fee_ids):
            raise TiveloError(
                ErrorCode.RULES_UNAVAILABLE, "A required fee has no known effective rule"
            )
        active = {key: rule for key, rule in selected.items() if rule.side in (side, "both")}
        if any(set(rule.included_fee_ids) & active.keys() for rule in active.values()):
            raise TiveloError(ErrorCode.DATA_CONFLICT, "An included fee would be charged twice")
        with localcontext(Context(prec=96)):
            notional = quantity * raw_price
            components = []
            for key, rule in sorted(active.items()):
                calculated = notional * rule.rate
                charge = max(calculated, rule.minimum)
                amount = (charge / rule.quantum).to_integral_value(
                    rounding=_ROUNDING[rule.rounding]
                ) * rule.quantum
                components.append(
                    FeeComponent(
                        fee_id=key,
                        amount=amount,
                        rate=rule.rate,
                        minimum_applied=calculated < rule.minimum,
                        rule_revision=rule.revision_id,
                        source_reference=rule.source_reference,
                    )
                )
            total = sum((part.amount for part in components), Decimal(0))
        return FeeQuote(
            currency=self.configuration.currency,
            notional=notional,
            components=tuple(components),
            total=total,
        )

    def fees(
        self,
        instrument: Instrument,
        side: str,
        quantity: Decimal,
        raw_price: Decimal,
        *,
        at: datetime,
    ) -> Decimal:
        return self.fee_breakdown(instrument, side, quantity, raw_price, at=at).total
