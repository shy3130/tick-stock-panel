"""Versioned facts. A record's effective time and knowledge time are distinct."""

from __future__ import annotations

from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, StrictBool, field_validator, model_validator

from .base import (
    CalendarDate,
    Contract,
    Currency,
    Digest,
    ExactDecimal,
    Key,
    NonNegativeDecimal,
    PositiveDecimal,
    RelativePath,
    UTCDateTime,
    fingerprint,
)
from .extension_fields import ExtensionField, validate_field_universe

AssetClass = Literal["equity", "fund", "index", "future"]
KnowledgeBasis = Literal["historical_versions", "latest_reconstruction", "unknown"]
PriceBasis = Literal["raw", "qfq", "hfq"]
QuantityUnit = Literal["share", "contract", "unit"]


class KnownDate(Contract):
    state: Literal["known", "unknown", "not_yet"]
    value: CalendarDate | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.state == "known") != (self.value is not None):
            raise ValueError("only known dates have a value")
        return self


class HistoricalRecord(Contract):
    valid_from: UTCDateTime
    valid_to: UTCDateTime | None = None
    available_at: UTCDateTime
    revision_id: Key
    source_id: Key

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("validity intervals are nonempty and left-closed/right-open")
        return self


class Instrument(HistoricalRecord):
    instrument_id: Key
    venue_id: Key
    market_id: Key
    asset_class: AssetClass
    instrument_type: Key
    display_symbol: Key
    name: Key
    quote_currency: Currency
    settlement_currency: Currency
    calendar_id: Key
    timezone: Key
    listing_date: KnownDate
    delisting_date: KnownDate
    tradable: StrictBool
    spec_revision: Key

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an installed IANA zone") from exc
        return value

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.asset_class == "index" and self.tradable:
            raise ValueError("an index is an analytical instrument, not a tradable security")
        listing, delisting = self.listing_date.value, self.delisting_date.value
        if listing is not None and delisting is not None and delisting < listing:
            raise ValueError("delisting precedes listing")
        return self


class SymbolMapping(HistoricalRecord):
    instrument_id: Key
    venue_id: Key
    symbol: Key


class _BarFields(Contract):
    schema_version: Literal["2.0"] = "2.0"
    instrument_id: Key
    timeframe: str = Field(pattern=r"^(1d|[1-9][0-9]*[smh])$")
    bar_start: UTCDateTime
    bar_end: UTCDateTime
    trading_date: CalendarDate
    session_id: Key
    open: ExactDecimal
    high: ExactDecimal
    low: ExactDecimal
    close: ExactDecimal
    volume: NonNegativeDecimal
    quantity_unit: QuantityUnit
    quote_currency: Currency
    turnover: NonNegativeDecimal | None = None
    vwap: ExactDecimal | None = None
    trade_count: int | None = Field(default=None, strict=True, ge=0)
    price_basis: str
    available_at: UTCDateTime
    ingested_at: UTCDateTime
    source_id: Key
    revision_id: Key
    is_final: StrictBool = True

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.bar_end <= self.bar_start:
            raise ValueError("bar_end must follow bar_start")
        if self.is_final and self.available_at < self.bar_end:
            raise ValueError("a final bar cannot be available before its interval ends")
        if self.ingested_at < self.available_at:
            raise ValueError("ingestion cannot precede declared availability")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("inconsistent OHLC bounds")
        return self


class Bar(_BarFields):
    price_basis: PriceBasis


class ProjectedBar(_BarFields):
    """A strategy-only view emitted by an extension. Never a snapshot market fact."""

    price_basis: Literal["extension"]


class OpeningObservation(Contract):
    """A separately sourced opening fact, never an early copy of a final daily bar.

    event_at names the calendar's execution opening. An auction may establish the
    price earlier; later publication remains later and is unavailable at the open.
    """

    schema_version: Literal["2.0"] = "2.0"
    instrument_id: Key
    event_at: UTCDateTime
    trading_date: CalendarDate
    session_id: Key
    state: Literal["tradable", "suspended", "no_trades", "not_listed", "delisted"]
    raw_open: ExactDecimal | None = None
    quote_currency: Currency
    reference_price: ExactDecimal | None = None
    reference_kind: Literal["previous_close", "exchange_ex_rights", "exchange_special"] | None = (
        None
    )
    available_at: UTCDateTime
    ingested_at: UTCDateTime
    source_id: Key
    revision_id: Key
    is_final: StrictBool = True

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.state == "tradable") != (self.raw_open is not None):
            raise ValueError("only tradable opening observations contain a raw opening price")
        if (self.reference_price is None) != (self.reference_kind is None):
            raise ValueError("an exchange reference needs both its price and kind")
        if self.ingested_at < self.available_at:
            raise ValueError("ingestion cannot precede declared availability")
        return self


class CorporateAction(Contract):
    action_id: Key
    instrument_id: Key
    action_type: Literal["split", "cash_dividend", "rights", "other"]
    effective_at: UTCDateTime
    available_at: UTCDateTime
    revision_id: Key
    source_id: Key
    cancelled: StrictBool = False
    quantity_ratio: PositiveDecimal | None = None
    cash_per_unit: NonNegativeDecimal | None = None
    currency: Currency | None = None
    record_at: UTCDateTime | None = None
    payment_at: UTCDateTime | None = None
    detail: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.action_type == "split":
            if self.quantity_ratio is None or self.cash_per_unit is not None:
                raise ValueError("a split requires a quantity ratio and is not a cash event")
        elif self.action_type == "cash_dividend":
            if (
                self.cash_per_unit is None
                or self.currency is None
                or self.quantity_ratio is not None
            ):
                raise ValueError("a cash dividend requires a currency and per-unit amount")
        elif not self.detail:
            raise ValueError("unmodeled actions must describe their unsupported semantics")
        if (self.cash_per_unit is None) != (self.currency is None):
            raise ValueError("cash amount and currency must be supplied together")
        return self


class AdjustmentFactor(Contract):
    factor_id: Key
    instrument_id: Key
    effective_at: UTCDateTime
    available_at: UTCDateTime
    value: PositiveDecimal
    definition_id: Key
    factor_kind: Literal["event_ratio", "cumulative_forward"]
    baseline_id: Key
    source_id: Key
    revision_id: Key
    cancelled: StrictBool = False


class CoverageIssue(Contract):
    instrument_id: Key | None = None
    reason: Literal[
        "missing",
        "suspended",
        "no_trades",
        "not_listed",
        "delisted",
        "not_published",
        "source_error",
        "cancelled",
        "unsupported",
        "no_events",
    ]
    detail: str = Field(min_length=1, max_length=1024)


class Coverage(Contract):
    status: Literal["complete", "partial", "empty", "error"]
    requested_units: int = Field(strict=True, ge=1)
    completed_units: int = Field(strict=True, ge=0)
    row_count: int = Field(strict=True, ge=0)
    issues: tuple[CoverageIssue, ...] = ()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.completed_units > self.requested_units:
            raise ValueError("completed units exceed request")
        if self.status == "complete" and (
            self.completed_units != self.requested_units or self.row_count == 0 or self.issues
        ):
            raise ValueError("complete requires all units, nonempty rows and no issues")
        if self.status == "partial" and (
            self.completed_units >= self.requested_units or self.row_count == 0 or not self.issues
        ):
            raise ValueError("partial requires missing units, some rows and an explanation")
        if self.status in ("empty", "error") and (self.row_count != 0 or not self.issues):
            raise ValueError("empty/error must have no rows and an explanation")
        if self.status == "error" and self.completed_units != 0:
            raise ValueError("a failed request cannot claim completed units")
        return self


class Artifact(Contract):
    path: RelativePath
    sha256: Digest
    size_bytes: int = Field(strict=True, ge=0)
    row_count: int = Field(strict=True, ge=0)


class DatasetScope(Contract):
    market_id: Key
    asset_class: AssetClass
    instrument_ids: tuple[Key, ...]
    extension_field: ExtensionField | None = Field(default=None, exclude_if=lambda v: v is None)
    start: UTCDateTime
    end: UTCDateTime
    timeframes: tuple[Key, ...] = ()
    bar_convention_id: Key | None = None
    coverage_basis: Literal["source_response", "validated_market_sessions"] = "source_response"

    @model_validator(mode="after")
    def coherent(self) -> Self:
        validate_field_universe(self.extension_field, self.instrument_ids)
        if self.end <= self.start:
            raise ValueError("dataset scope must be a nonempty half-open interval")
        if tuple(sorted(set(self.instrument_ids))) != self.instrument_ids:
            raise ValueError("scope instrument IDs must be unique and sorted")
        if tuple(sorted(set(self.timeframes))) != self.timeframes:
            raise ValueError("scope timeframes must be unique and sorted")
        return self


class DatasetContents(Contract):
    schema_version: Literal["2.0"] = "2.0"
    hash_encoding: Literal["tivelo-json-v1"] = "tivelo-json-v1"
    dataset_id: Key
    record_type: Literal[
        "instrument",
        "symbol_mapping",
        "bar",
        "corporate_action",
        "adjustment_factor",
        "opening_observation",
        "extension_value",
        "order_book_observation",
        "trade_print",
    ]
    knowledge_basis: KnowledgeBasis
    price_basis: PriceBasis | None = None
    source_ids: tuple[Key, ...] = Field(min_length=1)
    artifacts: tuple[Artifact, ...]
    coverage: Coverage
    normalization_version: Key
    source_selection_digest: Digest
    scope: DatasetScope | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.record_type == "extension_value") != bool(
            self.scope and self.scope.extension_field
        ):
            raise ValueError("extension datasets must lock a field definition in their scope")
        if self.record_type == "bar" and self.price_basis is None:
            raise ValueError("bar datasets must declare a price basis")
        if self.record_type in ("order_book_observation", "trade_print") and self.scope is None:
            raise ValueError("book and trade datasets must declare their instrument and time scope")
        if self.record_type != "bar" and self.price_basis is not None:
            raise ValueError("price_basis applies only to bar datasets")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("duplicate source ID")
        if len({a.path for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("duplicate artifact path")
        if sum(a.row_count for a in self.artifacts) != self.coverage.row_count:
            raise ValueError("artifact row counts disagree with coverage")
        return self


class DatasetVersion(Contract):
    version_id: Digest
    contents: DatasetContents

    @model_validator(mode="after")
    def verify_hash(self) -> Self:
        if self.version_id != fingerprint(self.contents):
            raise ValueError("dataset content fingerprint mismatch")
        return self

    @classmethod
    def create(cls, contents: DatasetContents) -> Self:
        return cls(version_id=fingerprint(contents), contents=contents)


class DatasetReference(Contract):
    dataset_id: Key
    version_id: Digest


class SnapshotContents(Contract):
    schema_version: Literal["2.0"] = "2.0"
    hash_encoding: Literal["tivelo-json-v1"] = "tivelo-json-v1"
    datasets: tuple[DatasetReference, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_datasets(self) -> Self:
        ids = [d.dataset_id for d in self.datasets]
        if ids != sorted(set(ids)):
            raise ValueError("snapshot dataset IDs must be unique and sorted")
        return self


class SnapshotManifest(Contract):
    snapshot_id: Digest
    contents: SnapshotContents

    @model_validator(mode="after")
    def verify_hash(self) -> Self:
        if self.snapshot_id != fingerprint(self.contents):
            raise ValueError("snapshot content fingerprint mismatch")
        return self

    @classmethod
    def create(cls, contents: SnapshotContents) -> Self:
        return cls(snapshot_id=fingerprint(contents), contents=contents)


class BarQuery(Contract):
    instrument_ids: tuple[Key, ...] = Field(min_length=1)
    timeframe: Key
    start: UTCDateTime
    end: UTCDateTime
    as_of: UTCDateTime
    price_basis: PriceBasis = "raw"

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.end <= self.start:
            raise ValueError("query range must be nonempty and left-closed/right-open")
        if len(set(self.instrument_ids)) != len(self.instrument_ids):
            raise ValueError("duplicate instrument ID")
        return self


class EventQuery(Contract):
    instrument_ids: tuple[Key, ...] = Field(min_length=1)
    start: UTCDateTime
    end: UTCDateTime
    as_of: UTCDateTime

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.end <= self.start or len(set(self.instrument_ids)) != len(self.instrument_ids):
            raise ValueError(
                "event queries need a nonempty half-open interval and unique instruments"
            )
        return self


class HistoryQuery(Contract):
    instrument_ids: tuple[Key, ...] = Field(min_length=1)
    effective_at: UTCDateTime
    as_of: UTCDateTime

    @model_validator(mode="after")
    def unique_instruments(self) -> Self:
        if len(set(self.instrument_ids)) != len(self.instrument_ids):
            raise ValueError("duplicate instrument ID")
        return self
