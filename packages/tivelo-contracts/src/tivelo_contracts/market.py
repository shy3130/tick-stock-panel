"""Historical execution terms and auditable rule outputs; no market defaults."""

from __future__ import annotations

from decimal import Context, Decimal, localcontext
from typing import Literal, Self

from pydantic import Field, model_validator

from .base import (
    CalendarDate,
    Contract,
    Currency,
    Key,
    NonNegativeDecimal,
    PositiveDecimal,
    UTCDateTime,
)
from .data import AssetClass, HistoricalRecord

OrderType = Literal["market", "limit"]
OddLotPolicy = Literal["all_remaining", "none", "remainder_once"]


class OrderQuantityTerms(Contract):
    """Quantity rules for one explicitly supported order type, in quoted units."""

    order_type: OrderType
    buy_minimum: PositiveDecimal
    buy_increment: PositiveDecimal
    buy_maximum: PositiveDecimal
    sell_minimum: PositiveDecimal
    sell_increment: PositiveDecimal
    sell_maximum: PositiveDecimal
    odd_lot_policy: OddLotPolicy

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.buy_maximum < self.buy_minimum or self.sell_maximum < self.sell_minimum:
            raise ValueError("order maxima cannot be below minima")
        if self.odd_lot_policy == "remainder_once" and self.sell_minimum != self.sell_increment:
            raise ValueError("remainder_once requires the sell minimum to equal the lot increment")
        return self


class TradingTerms(HistoricalRecord):
    spec_revision: Key
    market_id: Key
    venue_id: Key
    asset_class: AssetClass
    instrument_type: Key
    quantity_unit: Literal["share", "contract", "unit"]
    currency: Currency
    price_tick: PositiveDecimal
    buy_minimum: PositiveDecimal
    buy_increment: PositiveDecimal
    buy_maximum: PositiveDecimal
    sell_minimum: PositiveDecimal
    sell_increment: PositiveDecimal
    sell_maximum: PositiveDecimal
    odd_lot_policy: OddLotPolicy
    sell_lag_sessions: int = Field(strict=True, ge=0)
    price_limit_mode: Literal["relative_reference", "unlimited", "unknown"]
    limit_up_ratio: PositiveDecimal | None = None
    limit_down_ratio: PositiveDecimal | None = None
    source_reference: Key
    order_quantity_rules: tuple[OrderQuantityTerms, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.buy_maximum < self.buy_minimum or self.sell_maximum < self.sell_minimum:
            raise ValueError("order maxima cannot be below minima")
        if self.odd_lot_policy == "remainder_once" and self.sell_minimum != self.sell_increment:
            raise ValueError("remainder_once requires the sell minimum to equal the lot increment")
        if self.price_limit_mode == "relative_reference":
            if self.limit_up_ratio is None or self.limit_down_ratio is None:
                raise ValueError("relative limits require both ratios")
            if self.limit_down_ratio > 1:
                raise ValueError("positive-price market downside limit exceeds one")
        elif self.limit_up_ratio is not None or self.limit_down_ratio is not None:
            raise ValueError("unlimited/unknown bounds have no ratios")
        types = tuple(rule.order_type for rule in self.order_quantity_rules)
        if types != tuple(sorted(set(types))):
            raise ValueError("order quantity rules must have unique, sorted order types")
        return self


class ReferencePrice(Contract):
    instrument_id: Key
    trading_date: CalendarDate
    value: PositiveDecimal
    kind: Literal["previous_close", "exchange_ex_rights", "exchange_special"]
    available_at: UTCDateTime
    source_id: Key
    revision_id: Key


class TradingDay(Contract):
    calendar_id: Key
    trading_date: CalendarDate
    open_at: UTCDateTime
    close_at: UTCDateTime
    bar_convention_id: Key
    available_at: UTCDateTime

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.close_at <= self.open_at:
            raise ValueError("trading day closes strictly after its execution opening")
        return self


class PriceBounds(Contract):
    state: Literal["limited", "unlimited"]
    lower: PositiveDecimal | None = None
    upper: PositiveDecimal | None = None
    reference_revision: Key | None = None
    rule_revision: Key

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.state == "limited":
            if self.lower is None or self.upper is None or self.upper < self.lower:
                raise ValueError("limited bounds require ordered positive prices")
            if self.reference_revision is None:
                raise ValueError("limited bounds require a reference revision")
        elif (
            self.lower is not None or self.upper is not None or self.reference_revision is not None
        ):
            raise ValueError("unlimited bounds have no reference prices")
        return self


class FeeComponent(Contract):
    fee_id: Key
    amount: NonNegativeDecimal
    rate: NonNegativeDecimal
    minimum_applied: bool = Field(strict=True)
    rule_revision: Key
    source_reference: Key


class FeeQuote(Contract):
    currency: Currency
    notional: NonNegativeDecimal
    components: tuple[FeeComponent, ...]
    total: NonNegativeDecimal

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if len({item.fee_id for item in self.components}) != len(self.components):
            raise ValueError("fee components must be unique")
        with localcontext(Context(prec=96)):
            total = sum((item.amount for item in self.components), Decimal(0))
        if self.total != total:
            raise ValueError("fee total differs from component sum")
        return self
