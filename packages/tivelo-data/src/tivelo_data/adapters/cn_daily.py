"""Scope and instrument bindings for mainland-equity observations."""

from datetime import time
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator
from tivelo_contracts.base import CalendarDate, Contract, Key

from ..providers import SyncRequest

_VENUE_SUFFIX = {"XSHG": ".SH", "XSHE": ".SZ", "XBSE": ".BJ"}


class DailySymbol(Contract):
    instrument_id: Key
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    venue_id: Literal["XSHG", "XSHE", "XBSE"]

    @model_validator(mode="after")
    def venue_matches(self) -> Self:
        if not self.symbol.endswith(_VENUE_SUFFIX[self.venue_id]):
            raise ValueError("source symbol and exchange differ")
        return self


CN = ZoneInfo("Asia/Shanghai")


class CNSymbolRequest(Contract):
    request: SyncRequest
    expected_provider: str = Field(pattern=r"^[a-z0-9_]+$", max_length=128)
    symbols: tuple[DailySymbol, ...] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def source_scope(self) -> Self:
        ids = [entry.instrument_id for entry in self.symbols]
        if ids != sorted(self.request.instrument_ids) or len(
            {s.symbol for s in self.symbols}
        ) != len(ids):
            raise ValueError(
                "one unique source symbol per requested instrument is required in identity order"
            )
        if (self.request.market_id, self.request.asset_class) != ("cn.equity", "equity"):
            raise ValueError("Mainland equity capture requires cn.equity instruments")
        return self


class CNDailyRequest(CNSymbolRequest):
    calendar_version: Key

    @model_validator(mode="after")
    def daily_bars(self) -> Self:
        r = self.request
        if (r.record_type, r.timeframe) != ("bar", "1d") or r.price_basis not in {"raw", "qfq"}:
            raise ValueError("Daily capture requires mainland equity daily bars")
        return self


class CNRawDailyRequest(CNDailyRequest):
    @model_validator(mode="after")
    def raw_prices(self) -> Self:
        if self.request.price_basis != "raw":
            raise ValueError("Daily source capture requires raw prices")
        return self


class RepositoryDailyRequest(CNDailyRequest):
    calendar_version: Key | None = Field(default=None, exclude_if=lambda value: value is None)
    layout: Literal["daily_raw_v1", "enriched_v1"]


class CNRawMinuteRequest(CNSymbolRequest):
    calendar_version: Key

    @model_validator(mode="after")
    def minute_scope(self) -> Self:
        r = self.request
        if (r.record_type, r.timeframe, r.price_basis) != ("bar", "1m", "raw"):
            raise ValueError("Stock minute capture requires raw one-minute bars")
        if any(stamp.second or stamp.microsecond for stamp in (r.start, r.end)):
            raise ValueError("Minute capture requires whole minute boundaries")
        return self


class RepositoryMinuteRequest(CNRawMinuteRequest):
    layout: Literal["minute_v1"]
    minute_label: Literal["start", "end"]
    price_evidence: Literal["marked_raw", "legacy_close_anchor"] = "marked_raw"


class CNFactorRequest(CNSymbolRequest):
    representation: Literal["event_ratio", "cumulative_forward"]
    baseline_date: CalendarDate | None = None

    @model_validator(mode="after")
    def factor_scope(self) -> Self:
        r = self.request
        if r.record_type != "adjustment_factor":
            raise ValueError("Factor capture requires adjustment_factor records")
        if any(stamp.astimezone(CN).time() != time.min for stamp in (r.start, r.end)):
            raise ValueError("Factor intervals must use Beijing midnight boundaries")
        if (self.representation == "cumulative_forward") != (self.baseline_date is not None):
            raise ValueError("Only cumulative factors require a fixed baseline date")
        if self.baseline_date is not None and self.baseline_date > r.start.astimezone(CN).date():
            raise ValueError("The fixed baseline must be on or before the capture interval")
        return self


class RepositoryFactorRequest(CNFactorRequest):
    layout: Literal["event_factors_v1"]
