"""Immutable raw order-book observations and pinned as-of queries."""

from collections.abc import Iterable
from typing import Literal, Self

from pydantic import model_validator

from .base import CalendarDate, Contract, Currency, Key, PositiveDecimal, UTCDateTime
from .data import DatasetReference, DatasetVersion, EventQuery
from .errors import ErrorCode, TiveloError


class OrderBookObservation(Contract):
    """Displayed best prices at an explicit witness instant. Null means confirmed absent."""

    schema_version: Literal["2.0"] = "2.0"
    instrument_id: Key
    event_at: UTCDateTime
    trading_date: CalendarDate
    session_id: Key
    price_basis: Literal["raw"]
    quote_currency: Currency
    state: Literal["continuous", "halted"]
    best_bid: PositiveDecimal | None
    best_ask: PositiveDecimal | None
    last_trade: PositiveDecimal | None
    last_trade_at: UTCDateTime | None = None
    available_at: UTCDateTime
    ingested_at: UTCDateTime
    source_id: Key
    revision_id: Key

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if not self.event_at <= self.available_at <= self.ingested_at:
            raise ValueError("book event, availability and ingestion must be chronological")
        if (self.last_trade is None) != (self.last_trade_at is None):
            raise ValueError("last price and exchange trade time must occur together")
        if self.last_trade_at is not None and self.last_trade_at > self.event_at:
            raise ValueError("last trade follows the book observation")
        if (
            self.state == "continuous"
            and self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid >= self.best_ask
        ):
            raise ValueError("published continuous quotes must be uncrossed")
        return self


class OrderBookQuery(EventQuery):
    dataset: DatasetReference


def select_order_book_dataset(
    versions: Iterable[DatasetVersion], reference: DatasetReference
) -> DatasetVersion:
    matches = [v for v in versions if v.contents.dataset_id == reference.dataset_id]
    if not matches:
        raise TiveloError(ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Order-book dataset is absent")
    if len(matches) != 1 or matches[0].version_id != reference.version_id:
        raise TiveloError(
            ErrorCode.DATA_CONFLICT, "Order-book dataset differs from the selected version"
        )
    if matches[0].contents.record_type != "order_book_observation":
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Selected dataset does not contain order books"
        )
    return matches[0]
