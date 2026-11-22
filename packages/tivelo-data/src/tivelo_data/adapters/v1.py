"""TSP v1 OHLC boundary. Does not mutate legacy rows or application storage."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import MAX_EMAX, MIN_EMIN, Decimal, Overflow, Underflow, localcontext
from typing import Literal
from zoneinfo import ZoneInfo

from tivelo_contracts.base import utc_datetime
from tivelo_contracts.data import Bar, PriceBasis
from tivelo_contracts.errors import ErrorCode, TiveloError


def legacy_decimal(value) -> Decimal:
    if (
        isinstance(value, bool)
        or value is None
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Legacy financial value is missing or non-finite"
        )
    # The explicit v1 boundary is the only place that accepts historical float64
    # facts. Their decimal spelling is retained; no guessing or silent rounding.
    try:
        result = Decimal(str(value))
    except (ValueError, ArithmeticError) as exc:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid legacy financial value") from exc
    if not result.is_finite():
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Legacy financial value is non-finite")
    return result


def normalize_ratio(value, *, unit: Literal["ratio", "percent"]) -> Decimal:
    result = legacy_decimal(value)
    if unit == "percent":
        with localcontext() as context:
            context.prec = 80
            return result / Decimal(100)
    if unit == "ratio":
        return result
    raise ValueError("ratio unit must be explicit")


def scale_legacy_quantity(value: Decimal, multiplier: Decimal) -> Decimal:
    """Preserve every significant digit before the fact contract checks its fit."""
    if not value or multiplier == 1:
        return value
    with localcontext() as context:
        context.prec = max(80, len(value.as_tuple().digits) + len(multiplier.as_tuple().digits))
        context.Emax, context.Emin = MAX_EMAX, MIN_EMIN
        context.traps[Overflow] = context.traps[Underflow] = True
        return value * multiplier


def beijing_wall_time(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "v1 minute datetime must be a naive Beijing wall time"
        )
    return value.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)


@dataclass(frozen=True)
class V1BarAdapter:
    source_id: str
    input_schema: Literal["daily_raw_v1", "minute_raw_v1", "enriched_v1"]
    price_basis: PriceBasis
    timeframe: str
    currency: str
    quantity_unit: Literal["share", "contract", "unit"]
    volume_unit: Literal["quantity", "lot"]
    resolve_instrument: Callable[[str, date], str]
    ingested_at: datetime
    # Historical daily session bounds are supplied by a market/source adapter.
    # A date cannot identify the exchange calendar or auction convention alone.
    daily_bounds: Callable[[date], tuple[datetime, datetime, str]] | None = None
    lot_size: Callable[[str, date], Decimal] | None = None
    minute_label: Literal["start", "end"] | None = None
    availability_field: str | None = None

    def __post_init__(self):
        if self.input_schema not in ("daily_raw_v1", "minute_raw_v1", "enriched_v1"):
            raise ValueError("unknown legacy schema")
        if self.price_basis not in ("raw", "qfq") or (
            self.input_schema != "enriched_v1" and self.price_basis != "raw"
        ):
            raise TiveloError(
                ErrorCode.FACTOR_INCOMPATIBLE,
                "v1 raw/qfq columns cannot be relabeled as another basis",
            )
        if self.volume_unit not in ("lot", "quantity"):
            raise ValueError("unknown legacy volume unit")

    @property
    def knowledge_basis(self) -> str:
        # v1 does not retain the full revision history. Even an availability
        # field cannot by itself upgrade it to historical_versions.
        return "latest_reconstruction"

    @property
    def price_prefix(self) -> str:
        return "raw_" if self.input_schema == "enriched_v1" and self.price_basis == "raw" else ""

    def convert(
        self, row: Mapping, *, revision_id: str, minute_session_id: str | None = None
    ) -> Bar:
        ingested = utc_datetime(self.ingested_at)
        if self.input_schema == "minute_raw_v1":
            label = beijing_wall_time(row["datetime"])
            match = re.fullmatch(r"([1-9][0-9]*)([smh])", self.timeframe)
            if match is None or self.minute_label not in ("start", "end"):
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Minute duration and label convention are required"
                )
            delta = timedelta(seconds=int(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2]])
            start, end = (
                (label, label + delta) if self.minute_label == "start" else (label - delta, label)
            )
            trading_date = label.astimezone(ZoneInfo("Asia/Shanghai")).date()
            session = minute_session_id or "cn.v1.minute"
        else:
            value = row["date"]
            trading_date = date.fromisoformat(value) if isinstance(value, str) else value
            if (
                type(trading_date) is not date
                or self.daily_bounds is None
                or self.timeframe != "1d"
            ):
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID,
                    "Daily bars require an explicit calendar/session mapping",
                )
            start, end, session = self.daily_bounds(trading_date)
            start, end = utc_datetime(start), utc_datetime(end)
        instrument_id = self.resolve_instrument(str(row["symbol"]), trading_date)
        prefix = self.price_prefix
        columns = tuple(prefix + name for name in ("open", "high", "low", "close"))
        if any(column not in row or row[column] is None for column in columns):
            raise TiveloError(
                ErrorCode.RAW_PRICE_REQUIRED if prefix else ErrorCode.CONTRACT_INVALID,
                "Required original OHLC columns are missing",
            )
        if self.input_schema != "enriched_v1" and self.price_basis != "raw":
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Raw v1 schemas cannot be relabeled as adjusted"
            )
        volume = legacy_decimal(row["volume"])
        if self.volume_unit == "lot":
            if self.lot_size is None:
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Historical lot size is required")
            size = self.lot_size(instrument_id, trading_date)
            if not isinstance(size, Decimal) or not size.is_finite() or size <= 0:
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid historical lot size")
            volume = scale_legacy_quantity(volume, size)
        elif self.volume_unit != "quantity":
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Volume unit must be explicit")
        available = (
            utc_datetime(row[self.availability_field]) if self.availability_field else ingested
        )
        return Bar(
            instrument_id=instrument_id,
            timeframe=self.timeframe,
            bar_start=start,
            bar_end=end,
            trading_date=trading_date,
            session_id=session,
            source_id=self.source_id,
            revision_id=revision_id,
            open=legacy_decimal(row[columns[0]]),
            high=legacy_decimal(row[columns[1]]),
            low=legacy_decimal(row[columns[2]]),
            close=legacy_decimal(row[columns[3]]),
            volume=volume,
            quantity_unit=self.quantity_unit,
            quote_currency=self.currency,
            turnover=legacy_decimal(row["amount"]) if row.get("amount") is not None else None,
            price_basis=self.price_basis,
            available_at=available,
            ingested_at=ingested,
        )
