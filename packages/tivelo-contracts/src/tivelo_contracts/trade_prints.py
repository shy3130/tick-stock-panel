"""Exchange trade prints with immutable execution quantity and revision clocks."""

from collections.abc import Iterable
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .base import CalendarDate, Contract, Currency, Key, PositiveDecimal, UTCDateTime
from .data import DatasetReference, DatasetVersion, EventQuery, QuantityUnit
from .errors import ErrorCode, TiveloError

TradeSequence = Annotated[int, Field(strict=True, ge=0, lt=2**63)]


class TradePrint(Contract):
    """One execution, ordered by the source's exchange sequence for this security and day."""

    schema_version: Literal["2.0"] = "2.0"
    instrument_id: Key
    trading_date: CalendarDate
    sequence: TradeSequence
    event_at: UTCDateTime
    session_id: Key
    price_basis: Literal["raw"]
    quote_currency: Currency
    raw_price: PositiveDecimal
    quantity: PositiveDecimal
    quantity_unit: QuantityUnit
    aggressor_side: Literal["buy", "sell", "unknown"]
    available_at: UTCDateTime
    ingested_at: UTCDateTime
    source_id: Key
    revision_id: Key

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if not self.event_at <= self.available_at <= self.ingested_at:
            raise ValueError("trade, availability and ingestion must be chronological")
        return self


class TradePrintQuery(EventQuery):
    dataset: DatasetReference
    trading_date: CalendarDate
    sequence: TradeSequence | None = None


def select_trade_dataset(
    versions: Iterable[DatasetVersion], ref: DatasetReference
) -> DatasetVersion:
    matches = [v for v in versions if v.contents.dataset_id == ref.dataset_id]
    if not matches:
        raise TiveloError(ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Trade dataset is absent")
    if len(matches) != 1 or matches[0].version_id != ref.version_id:
        raise TiveloError(
            ErrorCode.DATA_CONFLICT, "Trade dataset differs from the selected version"
        )
    if matches[0].contents.record_type != "trade_print":
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Selected dataset does not contain trade prints"
        )
    return matches[0]
