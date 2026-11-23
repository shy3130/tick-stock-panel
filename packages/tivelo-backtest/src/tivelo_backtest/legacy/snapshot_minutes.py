"""Feed full raw stock minute sessions from a v2 reader into the v1 simulator."""

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Context
from typing import Literal, Protocol

import numpy as np
import polars as pl
from tivelo_contracts.base import utc_datetime
from tivelo_contracts.data import BarQuery
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.extension_fields import ExtensionFieldRef
from tivelo_contracts.protocols import DataReader

from ..research.signals import is_intraday_column
from ..snapshot import PortableSnapshotReader
from .minute_replay import CN_TIMEZONE

_MINUTE = timedelta(minutes=1)
_LOT_DECIMALS = Context(prec=38)
_PRICE_FIELDS = ("open", "high", "low", "close")
_PANEL_SCHEMA = {
    "symbol": pl.String,
    "datetime": pl.Datetime("us"),
    **dict.fromkeys((*_PRICE_FIELDS, "volume", "amount"), pl.Float64),
}


def read_legacy_minute_frame(
    reader: DataReader,
    query: BarQuery,
    *,
    symbols: Mapping[str, str] | None = None,
    minute_label: Literal["start", "end"] = "start",
    market_timezone=CN_TIMEZONE,
    signal_fields: Mapping[str, ExtensionFieldRef] | None = None,
) -> pl.DataFrame:
    """Read observed raw minutes in the old frame format, without building a calendar grid."""
    if query.timeframe != "1m" or query.price_basis != "raw" or minute_label not in {"start", "end"}:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Minute filters require raw 1m bars and a valid label")
    if signal_fields and not all(is_intraday_column(name) for name in signal_fields):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Minute signal aliases must be csgi_ columns")
    names = {instrument: instrument if symbols is None else symbols.get(instrument) for instrument in query.instrument_ids}
    if not all(names.values()) or len(set(names.values())) != len(names):
        raise TiveloError(ErrorCode.AMBIGUOUS_INSTRUMENT, "Minute frame requires unique stock bindings")
    seen = set()
    signal_rows = {}
    bars = reader.iter_bars(query) if type(reader) is PortableSnapshotReader else reader.bars(query)
    columns = {name: [] for name in _PANEL_SCHEMA}
    for bar in bars:
        if (
            bar.instrument_id not in names or not query.start <= bar.bar_start < query.end
            or (bar.timeframe, bar.price_basis, bar.quote_currency, bar.quantity_unit) != ("1m", "raw", "CNY", "share")
            or bar.bar_end != bar.bar_start + _MINUTE or bar.bar_start.second or bar.bar_start.microsecond
            or bar.trading_date != bar.bar_start.astimezone(market_timezone).date()
        ):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Minute reader returned a bar outside its contract")
        if not bar.is_final or max(bar.available_at, bar.bar_end) > query.as_of:
            raise TiveloError(ErrorCode.FUTURE_INFORMATION, "Minute bar is unfinished or unavailable")
        identity = bar.instrument_id, bar.bar_start
        if identity in seen:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Multiple observations for one minute bar")
        seen.add(identity)
        if signal_fields:
            signal_rows[bar.instrument_id, bar.bar_end] = len(columns["symbol"])
        label = bar.bar_start if minute_label == "start" else bar.bar_end
        columns["symbol"].append(names[bar.instrument_id])
        columns["datetime"].append(label.astimezone(market_timezone).replace(tzinfo=None))
        for name in _PRICE_FIELDS:
            columns[name].append(float(getattr(bar, name)))
        columns["volume"].append(float(bar.volume.scaleb(-2, context=_LOT_DECIMALS)))
        columns["amount"].append(float(bar.turnover) if bar.turnover is not None else None)
    frame = pl.DataFrame(columns, schema=_PANEL_SCHEMA)
    if signal_fields and signal_rows:
        frame = frame.with_columns(_minute_signal_columns(reader, signal_fields, signal_rows, query.as_of))
    return frame.sort("symbol", "datetime")


def _minute_signal_columns(reader, fields, rows, as_of):
    scan = getattr(reader, "iter_minute_signal_values", None)
    if not callable(scan):
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Reader lacks historical minute signal values")
    instruments = tuple(sorted({symbol for symbol, _ in rows}))
    cutoffs = [cutoff for _, cutoff in rows]
    start, end = min(cutoffs), max(cutoffs) + timedelta(microseconds=1)
    columns = []
    for name, reference in fields.items():
        values = [None] * len(rows)
        for item in scan(
            reference, instrument_ids=instruments, start=start, end=end, as_of=as_of,
        ):
            index = rows.get((item.instrument_id, item.effective_at))
            if index is not None:
                values[index] = item.bool_value
        if None in values:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT, f"Signal {name} lacks an on-time value for an observed minute",
            )
        columns.append(pl.Series(name, values, dtype=pl.Boolean))
    return columns


class MinuteSession(Protocol):
    @property
    def start(self) -> datetime: ...

    @property
    def end(self) -> datetime: ...


class RegularMinuteCalendar(Protocol):
    def regular_sessions(
        self, trading_date: date, *, as_of: datetime
    ) -> tuple[MinuteSession, ...]: ...


def _session_grid(calendar, day, as_of):
    result = {}
    previous_end = None
    for session in calendar.regular_sessions(day, as_of=as_of):
        start, end = utc_datetime(session.start), utc_datetime(session.end)
        if (
            end <= start
            or (previous_end is not None and start < previous_end)
            or any(t.second or t.microsecond for t in (start, end))
        ):
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Invalid regular minute session boundaries"
            )
        if end > as_of:
            raise TiveloError(ErrorCode.FUTURE_INFORMATION, "Regular minute session is unfinished")
        while start < end:
            result[start] = len(result)
            start += _MINUTE
        previous_end = end
    if not result:
        raise TiveloError(
            ErrorCode.DATA_COVERAGE_INSUFFICIENT, "No regular session on the requested fill date"
        )
    return result


class LegacySnapshotMinutes:
    """A LegacySimulator minute_loader. Each symbol binds a stable ID and a market calendar."""

    def __init__(
        self,
        reader: DataReader,
        instruments: dict[str, tuple[str, RegularMinuteCalendar]],
        *,
        as_of: datetime,
    ):
        self.reader = reader
        self.instruments = dict(instruments)
        self.as_of = utc_datetime(as_of)
        ids = [instrument for instrument, _ in self.instruments.values()]
        if len(set(ids)) != len(ids) or not all(ids):
            raise TiveloError(
                ErrorCode.AMBIGUOUS_INSTRUMENT, "Minute symbols need unique instrument bindings"
            )

    def __call__(self, symbols, dates, asset_type):
        if asset_type != "stock":
            raise TiveloError(
                ErrorCode.UNSUPPORTED_ASSET, "This v1 minute loader expects mainland stocks"
            )
        if not symbols or not dates:
            return {}
        if set(symbols) - self.instruments.keys():
            raise TiveloError(
                ErrorCode.AMBIGUOUS_INSTRUMENT, "No instrument binding for requested stock symbol"
            )
        bindings = {self.instruments[symbol][0]: symbol for symbol in symbols}
        result = {}
        for day_string in sorted(dates):
            day = date.fromisoformat(day_string)
            calendar_grids = {}
            grids = {}
            arrays = {}
            seen = {}
            for instrument, symbol in bindings.items():
                calendar = self.instruments[symbol][1]
                if id(calendar) not in calendar_grids:
                    calendar_grids[id(calendar)] = _session_grid(calendar, day, self.as_of)
                grid = calendar_grids[id(calendar)]
                grids[instrument] = grid
                arrays[instrument] = np.empty((len(grid), 6), dtype=np.float64)
                seen[instrument] = np.zeros(len(grid), dtype=bool)
            query = BarQuery(
                instrument_ids=tuple(bindings),
                timeframe="1m",
                price_basis="raw",
                start=min(min(grid) for grid in grids.values()),
                end=max(max(grid) for grid in grids.values()) + _MINUTE,
                as_of=self.as_of,
            )
            for bar in self.reader.bars(query):
                if bar.instrument_id not in grids:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Minute reader returned an unrequested stock"
                    )
                index = grids[bar.instrument_id].get(bar.bar_start)
                if index is None:
                    continue
                if seen[bar.instrument_id][index]:
                    raise TiveloError(
                        ErrorCode.DATA_CONFLICT, "Multiple raw observations for a required minute"
                    )
                if (bar.price_basis, bar.timeframe, bar.quantity_unit, bar.quote_currency) != (
                    "raw",
                    "1m",
                    "share",
                    "CNY",
                ):
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Minute fill requires raw CNY share bars"
                    )
                if bar.trading_date != day or bar.bar_end != bar.bar_start + _MINUTE:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Minute bar and regular session disagree"
                    )
                if not bar.is_final or bar.available_at > self.as_of:
                    raise TiveloError(
                        ErrorCode.FUTURE_INFORMATION, "Minute bar is not final and visible"
                    )
                if (
                    bar.turnover is None
                    or bar.turnover <= 0
                    or bar.volume <= 0
                    or min(bar.open, bar.high, bar.low, bar.close) <= 0
                ):
                    raise TiveloError(
                        ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                        "Minute fill needs traded prices, quantity and turnover",
                    )
                lots = bar.volume.scaleb(-2, context=_LOT_DECIMALS)
                arrays[bar.instrument_id][index] = (
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    lots,
                    bar.turnover,
                )
                seen[bar.instrument_id][index] = True
            for instrument, symbol in bindings.items():
                if not seen[instrument].all():
                    raise TiveloError(
                        ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                        "Snapshot does not cover every regular minute of the fill date",
                    )
                arrays[instrument].setflags(write=False)
                result[symbol, day_string] = arrays[instrument]
        return result
