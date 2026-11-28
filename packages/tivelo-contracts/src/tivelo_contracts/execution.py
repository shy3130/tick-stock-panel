"""Requests and locked, immutable inputs for reproducible engine execution."""

from __future__ import annotations

from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .base import (
    CalendarDate,
    Contract,
    Currency,
    Digest,
    ExactDecimal,
    Key,
    NonNegativeDecimal,
    ParametersJSON,
    PositiveDecimal,
    UnitRatio,
    UTCDateTime,
    fingerprint,
)
from .data import Bar, Instrument, OpeningObservation, ProjectedBar, SnapshotManifest
from .extension_fields import (
    ExtensionFieldRef,
    ExtensionValue,
    FormulaField,
    validate_formula_fields,
)
from .market import FeeComponent, FeeQuote, OrderType


class DefinitionReference(Contract):
    definition_id: Key
    version: Key
    sha256: Digest
    parameters_json: ParametersJSON = "{}"


class PackageReference(Contract):
    name: Key
    version: Key
    sha256: Digest
    artifact_kind: Literal["wheel", "installed_tree_v1", "source_tree_v1", "frozen_image_v1"] = Field(
        default="wheel", exclude_if=lambda value: value == "wheel"
    )


class PriceViewSpec(Contract):
    provider_id: Key
    mode: Literal["raw", "qfq", "hfq", "extension"]
    anchor_at: UTCDateTime | None = None
    factor_definition_id: Key | None = None
    factor_baseline_id: Key | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        factor_fields = (self.factor_definition_id, self.factor_baseline_id)
        if self.mode == "raw" and (self.anchor_at is not None or any(factor_fields)):
            raise ValueError("raw prices have no adjustment anchor or factor definition")
        if self.mode in ("qfq", "hfq") and (self.anchor_at is None or not all(factor_fields)):
            raise ValueError("static adjusted prices require a fixed anchor and factor definition")
        return self


SequenceText = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,18})$")]


class TradeSessionIndex(Contract):
    instrument_id: Key
    trading_date: CalendarDate
    sequences: tuple[SequenceText, ...] = Field(min_length=1)
    confirmed_through_at: UTCDateTime

    @model_validator(mode="after")
    def ordered(self) -> Self:
        numbers = tuple(map(int, self.sequences))
        if numbers[-1] >= 2**63 or any(left >= right for left, right in pairwise(numbers)):
            raise ValueError("trade sequences must be increasing")
        return self


class ContinuousExecution(Contract):
    book_dataset_id: Key
    trade_dataset_id: Key
    sessions: tuple[TradeSessionIndex, ...] = Field(min_length=1)
    observe_at: tuple[UTCDateTime, ...] = ()
    decide_at: tuple[UTCDateTime, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        identities = [(s.trading_date, s.instrument_id) for s in self.sessions]
        if identities != sorted(set(identities)) or self.book_dataset_id == self.trade_dataset_id:
            raise ValueError("continuous input sessions and datasets must have unique identities")
        if any(clock != tuple(sorted(set(clock))) for clock in (self.observe_at, self.decide_at)):
            raise ValueError("continuous clock times must be unique and increasing")
        return self


class ExecutionModel(Contract):
    timeframe: Key
    signal_at: Literal["bar_close", "scheduled"] = "bar_close"
    fill_at: Literal["next_open", "legacy_same_close", "witnessed_trade"] = "next_open"
    slippage_bps: NonNegativeDecimal = "0"
    intrabar_priority: Literal["stop_first", "take_first"] = "stop_first"
    end_policy: Literal["mark_to_market", "attempt_liquidation"] = "mark_to_market"
    missing_data: Literal["reject", "hold_last_valuation"] = "reject"
    position_policy_failure: Literal["reject", "equal_weight_with_diagnostic"] = "reject"
    order_expiry: Literal["next_session", "good_till_cancelled", "confirmed_end_of_day"] = (
        "next_session"
    )
    decision_delay_seconds: int = Field(
        default=0, strict=True, ge=0, exclude_if=lambda value: value == 0
    )
    continuous: ContinuousExecution | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def execution_clock(self) -> Self:
        continuous = self.continuous is not None
        if (
            continuous != (self.fill_at == "witnessed_trade")
            or continuous != (self.signal_at == "scheduled")
            or continuous != (self.order_expiry == "confirmed_end_of_day")
            or (
                continuous
                and (
                    self.slippage_bps != 0
                    or self.decision_delay_seconds != 0
                    or self.intrabar_priority != "stop_first"
                )
            )
        ):
            raise ValueError("witnessed trades require the scheduled clock and confirmed expiry")
        return self


class BacktestSpec(Contract):
    spec_version: Literal["2.0"] = "2.0"
    semantics_version: Literal["strict.daily.v1", "legacy.tsp.v1", "strict.continuous.v1"]
    snapshot_id: Digest
    universe: tuple[Key, ...] = Field(min_length=1)
    start: CalendarDate
    end: CalendarDate
    warmup_start: CalendarDate | None = Field(default=None, exclude_if=lambda value: value is None)
    strategy: DefinitionReference
    dependencies: tuple[DefinitionReference, ...] = ()
    extension_fields: tuple[ExtensionFieldRef, ...] = Field(
        default=(), max_length=64, exclude_if=lambda v: not v
    )
    market_profile: DefinitionReference
    account_profile: DefinitionReference
    price_view: PriceViewSpec
    execution: ExecutionModel
    accounting_model: Literal["legacy_adjusted_curve", "event_ledger"]
    knowledge_requirement: Literal["historical_versions", "latest_reconstruction"]
    initial_capital: PositiveDecimal
    base_currency: Currency
    seed: int | None = Field(default=None, strict=True, ge=0)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.end < self.start:
            raise ValueError("end precedes start")
        if self.warmup_start is not None and self.warmup_start > self.start:
            raise ValueError("warmup cannot begin after the tradable period starts")
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("duplicate universe member")
        if (self.semantics_version == "strict.continuous.v1") != (
            self.execution.continuous is not None
        ):
            raise ValueError("continuous inputs require their own execution semantics")
        if self.execution.continuous is not None and (
            self.accounting_model != "event_ledger" or self.knowledge_requirement != "historical_versions"
        ):
            raise ValueError("continuous execution requires historical versions and an event ledger")
        if self.extension_fields and (
            self.semantics_version != "strict.daily.v1"
            or len({ref.dataset_id for ref in self.extension_fields}) != len(self.extension_fields)
            or len({ref.field_sha256 for ref in self.extension_fields})
            != len(self.extension_fields)
        ):
            raise ValueError(
                "strict research field bindings must have unique datasets and definitions"
            )
        ids = [d.definition_id for d in (self.strategy, *self.dependencies)]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate strategy/dependency definition")
        if self.semantics_version == "strict.daily.v1":
            if self.execution.timeframe != "1d" or self.execution.fill_at != "next_open":
                raise ValueError("strict daily close signals execute at a later session open")
            if self.knowledge_requirement != "historical_versions":
                raise ValueError("strict semantics require historical information versions")
        return self


class RuntimeReference(Contract):
    python_version: Key
    python_abi: Key
    platform_tag: Key
    dependency_lock_sha256: Digest
    python_sha256: Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    stdlib_sha256: Digest | None = Field(default=None, exclude_if=lambda value: value is None)


class MarketAdapterSpec(Contract):
    """An explicitly selected installed entry point and its immutable configuration."""

    adapter_id: Key
    distribution: str = Field(
        strict=True, max_length=256, pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
    )
    configuration_json: ParametersJSON = "{}"


class ScaleProof(Contract):
    """Core-verified bounded program; never an arbitrary Python self-attestation."""

    verifier_version: Literal["tivelo.scale.controlled.v1", "tivelo.scale.controlled.v1.1"] = (
        "tivelo.scale.controlled.v1"
    )
    field_operands: tuple[FormulaField, ...] = Field(
        default=(), max_length=64, exclude_if=lambda values: not values
    )
    strategy: DefinitionReference
    program_sha256: Digest
    numeric_semantics: Literal["anchor_cancelled_float64_v1", "projected_prices_float64_v1"] = (
        "anchor_cancelled_float64_v1"
    )
    scope: Literal["entry_exit_score_selection_fixed_weight_no_price_outputs"] = (
        "entry_exit_score_selection_fixed_weight_no_price_outputs"
    )

    @model_validator(mode="after")
    def field_verifier_matches(self) -> Self:
        if bool(self.field_operands) != (self.verifier_version == "tivelo.scale.controlled.v1.1"):
            raise ValueError("field operands require the field-aware verifier")
        validate_formula_fields(self.field_operands)
        return self


class ExtensionPin(Contract):
    """Identity of a verified native capability used by this run."""

    extension_id: Key
    version: Key
    kind: Literal["price_view", "dataset_provider"]
    capability_id: Key
    feature_id: Key | None = None
    archive_sha256: Digest
    entry_sha256: Digest
    api_version: int = Field(strict=True, ge=1)
    runtime_generation: Key


class RunLockContents(Contract):
    lock_version: Literal["1.0"] = "1.0"
    hash_encoding: Literal["tivelo-json-v1"] = "tivelo-json-v1"
    spec: BacktestSpec
    snapshot: SnapshotManifest
    packages: tuple[PackageReference, ...] = Field(min_length=1)
    extensions: tuple[ExtensionPin, ...] = Field(default=(), exclude_if=lambda value: not value)
    extension_requirements: tuple[Key, ...] = Field(
        default=(), max_length=128, exclude_if=lambda value: not value
    )
    runtime: RuntimeReference
    verifier_version: Key
    market_adapter: MarketAdapterSpec | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    scale_proof: ScaleProof | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.spec.snapshot_id != self.snapshot.snapshot_id:
            raise ValueError("RunLock snapshot differs from requested snapshot")
        if self.scale_proof is not None and self.scale_proof.strategy != self.spec.strategy:
            raise ValueError("scale proof does not reference the locked strategy")
        names = [p.name for p in self.packages]
        if names != sorted(set(names)):
            raise ValueError("locked package names must be unique and sorted")
        capabilities = [(p.kind, p.capability_id) for p in self.extensions]
        if self.extension_requirements and (
            not self.extensions
            or self.extension_requirements != tuple(sorted(set(self.extension_requirements)))
        ):
            raise ValueError("extension requirements must be unique, sorted and bound to a release")
        if capabilities != sorted(set(capabilities)):
            raise ValueError("locked extension capabilities must be unique and sorted")
        releases = {}
        for pin in self.extensions:
            release = (pin.version, pin.archive_sha256)
            if releases.setdefault(pin.extension_id, release) != release:
                raise ValueError("a run must use one immutable version of each extension")
        views = [p.capability_id for p in self.extensions if p.kind == "price_view"]
        expected = (
            [self.spec.price_view.provider_id] if self.spec.price_view.mode == "extension" else []
        )
        if views != expected:
            raise ValueError("price-view extension pin differs from the requested provider")
        return self


class RunLock(Contract):
    lock_id: Digest
    contents: RunLockContents

    @model_validator(mode="after")
    def verify_hash(self) -> Self:
        if self.lock_id != fingerprint(self.contents):
            raise ValueError("RunLock fingerprint mismatch")
        return self

    @classmethod
    def create(cls, contents: RunLockContents) -> Self:
        return cls(lock_id=fingerprint(contents), contents=contents)


class PriceScale(Contract):
    provider_id: Key
    mode: Key
    anchor_at: UTCDateTime | None
    decision_at: UTCDateTime
    instrument_id: Key
    currency: Currency
    raw_multiplier: PositiveDecimal
    run_lock_id: Digest | None = Field(default=None, exclude_if=lambda value: value is None)


class PriceValue(Contract):
    value: ExactDecimal
    scale: PriceScale


class Position(Contract):
    instrument_id: Key
    quantity: NonNegativeDecimal
    sellable_quantity: NonNegativeDecimal
    cost: ExactDecimal
    currency: Currency

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.sellable_quantity > self.quantity:
            raise ValueError("sellable quantity exceeds the position")
        return self


class CashBalance(Contract):
    currency: Currency
    total: ExactDecimal
    available: ExactDecimal
    receivable: NonNegativeDecimal = "0"
    payable: NonNegativeDecimal = "0"


class Portfolio(Contract):
    cash: tuple[CashBalance, ...]
    positions: tuple[Position, ...]

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({c.currency for c in self.cash}) != len(self.cash):
            raise ValueError("duplicate cash currency")
        if len({p.instrument_id for p in self.positions}) != len(self.positions):
            raise ValueError("duplicate position")
        return self


class StrategyContext(Contract):
    now: UTCDateTime
    history: tuple[Bar | ProjectedBar, ...]
    instruments: tuple[Instrument, ...]
    portfolio: Portfolio
    parameters_json: ParametersJSON
    openings: tuple[OpeningObservation, ...] = Field(default=(), exclude_if=lambda value: not value)
    extension_values: tuple[ExtensionValue, ...] = Field(default=(), exclude_if=lambda v: not v)
    price_scales: tuple[PriceScale, ...] = Field(default=(), exclude_if=lambda values: not values)

    @model_validator(mode="after")
    def causal(self) -> Self:
        if len({scale.instrument_id for scale in self.price_scales}) != len(self.price_scales) or any(
            scale.decision_at != self.now for scale in self.price_scales
        ):
            raise ValueError("strategy access requires one current price scale per instrument")
        if any(
            row.available_at > self.now
            or row.effective_at > self.now
            or (row.valid_until is not None and self.now >= row.valid_until)
            for row in self.extension_values
        ):
            raise ValueError("strategy context contains unavailable or expired extension values")
        if any(
            o.available_at > self.now or o.event_at > self.now or not o.is_final
            for o in self.openings
        ):
            raise ValueError("strategy context contains unavailable opening facts")
        if any(
            b.available_at > self.now or b.bar_end > self.now or not b.is_final
            for b in self.history
        ):
            raise ValueError("strategy history contains unavailable or unfinished bars")
        if any(
            i.available_at > self.now
            or i.valid_from > self.now
            or (i.valid_to is not None and self.now >= i.valid_to)
            for i in self.instruments
        ):
            raise ValueError("strategy instruments are not known/effective at the decision")
        return self


class Signal(Contract):
    instrument_id: Key
    decision_at: UTCDateTime
    side: Literal["buy", "sell"]
    score: ExactDecimal
    source_id: Key
    target_weight: UnitRatio | None = None
    limit_price: PriceValue | None = None
    limit_quantity: PositiveDecimal | None = Field(default=None, exclude_if=lambda value: value is None)

    @property
    def order_type(self) -> OrderType:
        return "limit" if self.limit_price is not None else "market"

    @model_validator(mode="after")
    def matching_scale(self) -> Self:
        if self.limit_quantity is not None and (
            self.limit_price is None or self.target_weight is not None
        ):
            raise ValueError("an explicit quantity requires a limit price and no target weight")
        if self.limit_price is not None and (
            self.limit_price.scale.decision_at != self.decision_at
            or self.limit_price.scale.instrument_id != self.instrument_id
        ):
            raise ValueError("signal price scale has a different instrument or decision time")
        return self


class SignalBatch(Contract):
    decision_at: UTCDateTime
    signals: tuple[Signal, ...]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if any(s.decision_at != self.decision_at for s in self.signals):
            raise ValueError("signals in a batch must share a decision time")
        if len({s.instrument_id for s in self.signals}) != len(self.signals):
            raise ValueError("only one signal per instrument is supported in a batch")
        return self


class OrderEvent(Contract):
    order_id: Key
    instrument_id: Key
    side: Literal["buy", "sell"]
    status: Literal[
        "created", "accepted", "partially_filled", "filled", "rejected", "cancelled", "expired"
    ]
    decision_at: UTCDateTime
    submitted_at: UTCDateTime
    eligible_at: UTCDateTime
    filled_at: UTCDateTime | None = None
    event_at: UTCDateTime | None = None
    requested_quantity: PositiveDecimal
    filled_quantity: NonNegativeDecimal = "0"
    raw_fill_price: ExactDecimal | None = None
    fee: NonNegativeDecimal = "0"
    fee_components: tuple[FeeComponent, ...] = ()
    currency: Currency
    reason: Key | None = None
    order_type: OrderType | None = Field(default=None, exclude_if=lambda value: value is None)
    raw_limit_price: ExactDecimal | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.order_type == "limit") != (self.raw_limit_price is not None):
            raise ValueError("only a recorded limit order has a raw limit price")
        if self.filled_quantity > 0 and self.raw_limit_price is not None:
            if self.raw_fill_price is not None and (
                (self.side == "buy" and self.raw_fill_price > self.raw_limit_price)
                or (self.side == "sell" and self.raw_fill_price < self.raw_limit_price)
            ):
                raise ValueError("execution price violates the recorded order limit")
        if self.fee_components:
            FeeQuote(
                currency=self.currency, notional="0", components=self.fee_components, total=self.fee
            )
        if self.submitted_at < self.decision_at or self.eligible_at < self.submitted_at:
            raise ValueError("order timestamps are out of sequence")
        if self.event_at is not None and (
            self.event_at < self.submitted_at
            or (self.filled_at is not None and self.event_at < self.filled_at)
        ):
            raise ValueError("order event precedes its submission or fill")
        if self.filled_quantity > self.requested_quantity:
            raise ValueError("overfilled order")
        if self.filled_quantity > 0:
            if (
                self.filled_at is None
                or self.raw_fill_price is None
                or self.filled_at < self.eligible_at
            ):
                raise ValueError("fills require a raw price and an eligible execution time")
        elif (
            self.filled_at is not None
            or self.raw_fill_price is not None
            or self.fee != 0
            or self.fee_components
        ):
            raise ValueError("unfilled orders have no fill timestamp, price or fee")
        if self.status == "filled" and self.filled_quantity != self.requested_quantity:
            raise ValueError("filled status requires the full requested quantity")
        if (
            self.status == "partially_filled"
            and not 0 < self.filled_quantity < self.requested_quantity
        ):
            raise ValueError("partial status requires a partial quantity")
        if self.status in ("created", "accepted", "rejected") and self.filled_quantity != 0:
            raise ValueError("this order state cannot contain executions")
        return self


class EquityPoint(Contract):
    at: UTCDateTime
    equity: ExactDecimal
    currency: Currency


class LedgerEntry(Contract):
    entry_id: Key
    at: UTCDateTime
    kind: Literal["buy", "sell", "split", "dividend_accrual", "dividend_payment"]
    reference_id: Key
    instrument_id: Key
    currency: Currency
    cash_delta: ExactDecimal
    quantity_delta: ExactDecimal
    receivable_delta: ExactDecimal = "0"

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.kind == "buy" and not (
            self.cash_delta < 0 and self.quantity_delta > 0 and self.receivable_delta == 0
        ):
            raise ValueError("buy ledger entry must debit cash and credit quantity")
        if self.kind == "sell" and not (self.quantity_delta < 0 and self.receivable_delta == 0):
            raise ValueError("sell ledger entry must debit quantity without changing receivables")
        if self.kind == "split" and (self.cash_delta != 0 or self.receivable_delta != 0):
            raise ValueError("split ledger entry changes only quantity")
        if self.kind == "dividend_accrual" and not (
            self.cash_delta == 0 and self.quantity_delta == 0 and self.receivable_delta >= 0
        ):
            raise ValueError("dividend accrual credits only receivables")
        if self.kind == "dividend_payment" and not (
            self.cash_delta >= 0
            and self.quantity_delta == 0
            and self.receivable_delta == self.cash_delta.copy_negate()
        ):
            raise ValueError("dividend payment transfers receivables into cash")
        return self


class ExecutionDiagnostic(Contract):
    code: Key
    at: UTCDateTime
    instrument_id: Key | None = None


class BacktestResult(Contract):
    result_version: Literal["2.0"] = "2.0"
    lock_id: Digest
    semantics_version: Key
    accounting_model: Key
    orders: tuple[OrderEvent, ...]
    equity: tuple[EquityPoint, ...]
    final_portfolio: Portfolio
    ledger: tuple[LedgerEntry, ...] = ()
    diagnostics: tuple[Key, ...] = ()
    diagnostic_events: tuple[ExecutionDiagnostic, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def immutable_order_parameters(self) -> Self:
        parameters = {}
        for order in self.orders:
            value = order.order_type, order.raw_limit_price
            if order.order_id in parameters and parameters[order.order_id] != value:
                raise ValueError("order type and raw limit cannot change within an order history")
            parameters[order.order_id] = value
        return self
