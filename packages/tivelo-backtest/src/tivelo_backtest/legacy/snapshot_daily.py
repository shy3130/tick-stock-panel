"""Read daily snapshot bars as the existing TSP compatibility panel."""

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, time
from decimal import Context

import polars as pl
from tivelo_contracts.data import BarQuery, HistoryQuery
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import PriceViewSpec
from tivelo_contracts.extension_fields import ExtensionFieldRef, resolve_field
from tivelo_contracts.protocols import DataReader, ExtensionFieldReader

from ..controlled_fields import extension_observations
from ..price_views import StaticPriceView
from ..research.matrix_selection import is_risk_warning_name
from ..snapshot import PortableSnapshotReader

_PRICE_COLUMNS = ("open", "high", "low", "close")
_PANEL_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    **{name: pl.Float64 for name in (*_PRICE_COLUMNS, "volume", "amount")},
}
_RESERVED_FIELDS = set(_PANEL_SCHEMA) | {
    "name", "score", "datetime", "is_st", *(f"raw_{name}" for name in _PRICE_COLUMNS)
}
_FIELD_UNITS = {"total_shares": "share", "float_shares": "share", "turnover_rate": "percent"}
_VALUE_FIELDS = {"int": "int_value", "float": "decimal_value", "bool": "bool_value"}


def _checked_daily_bars(bars, query, names):
    identities = set()
    for bar in bars:
        if (
            bar.instrument_id not in names
            or bar.timeframe != query.timeframe
            or bar.price_basis != query.price_basis
            or not query.start <= bar.bar_start < query.end
        ):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Daily reader returned an unrelated bar")
        if not bar.is_final or max(bar.available_at, bar.bar_end) > query.as_of:
            raise TiveloError(
                ErrorCode.FUTURE_INFORMATION, "Daily bar is unfinished or unavailable"
            )
        if bar.quote_currency != "CNY" or bar.quantity_unit != "share":
            raise TiveloError(
                ErrorCode.UNSUPPORTED_ASSET, "Legacy panels expect CNY prices and shares"
            )
        key = bar.instrument_id, bar.trading_date
        if key in identities:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Legacy panel requires one bar per symbol and date"
            )
        identities.add(key)
        yield bar


def _daily_fields(reader, groups, row_count, references, include_st, include_names):
    if references and not isinstance(reader, ExtensionFieldReader):
        raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Reader lacks extension fields")
    values = {name: [None] * row_count for name in references}
    if include_st:
        values["is_st"] = [None] * row_count
    if include_names:
        values["name"] = [None] * row_count
    datasets = {}
    for name, reference in references.items():
        dataset = resolve_field(reader.datasets, reference)
        field = dataset.contents.scope.extension_field
        unit = _FIELD_UNITS.get(name)
        if field.value_type not in _VALUE_FIELDS or (
            unit is not None and (
                field.unit != unit or field.granularity != "instrument" or field.value_type == "bool"
            )
        ):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, f"Incompatible field type or unit for {name}")
        datasets[name] = dataset

    for cutoff, rows in groups.items():
        indices = {instrument_id: index for index, instrument_id in rows}
        if len(indices) != len(rows):
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Repeated daily closing time for an instrument")
        instruments = tuple(indices)
        if include_st or include_names:
            query = HistoryQuery(instrument_ids=instruments, effective_at=cutoff, as_of=cutoff)
            seen = set()
            for item in reader.instruments(query):
                if (
                    item.instrument_id not in indices or item.instrument_id in seen
                    or item.available_at > cutoff or item.valid_from > cutoff
                    or (item.valid_to is not None and item.valid_to <= cutoff)
                ):
                    raise TiveloError(ErrorCode.DATA_CONFLICT, "Invalid visible instrument state")
                seen.add(item.instrument_id)
                index = indices[item.instrument_id]
                if include_names:
                    values["name"][index] = item.name
                if include_st:
                    values["is_st"][index] = float(is_risk_warning_name(item.name))
        for name, reference in references.items():
            dataset = datasets[name]
            field = dataset.contents.scope.extension_field
            subjects = () if field.granularity == "market" else instruments
            column = values[name]
            for item in extension_observations(reader, ((reference, dataset, subjects),), cutoff):
                subject = item.instrument_id
                value = getattr(item, _VALUE_FIELDS[field.value_type])
                if value is None:
                    continue
                if subject is not None:
                    column[indices[subject]] = float(value)
                else:
                    for index in indices.values():
                        column[index] = float(value)
    return [
        pl.Series(name, column, dtype=pl.String if name == "name" else pl.Float64)
        for name, column in values.items()
    ]


def read_legacy_daily_panel(
    reader: DataReader,
    query: BarQuery,
    *,
    symbols: Mapping[str, str] | None = None,
    price_view: PriceViewSpec | None = None,
    extension_fields: Mapping[str, ExtensionFieldRef] | None = None,
    include_st: bool = False,
    include_names: bool = False,
    field_cutoff_time: time | None = None,
) -> pl.DataFrame:
    """Use observed days and an explicit snapshot cutoff for a legacy research run.

    `symbols` maps stable instrument IDs to the original TSP symbols.
    A requested fixed price view is applied once at the cutoff, preserving
    unadjusted OHLC for formulas that use raw prices.
    Supplementary fields use the recorded bar end or an explicit local time
    with its UTC offset on each observed trading date.
    """
    if query.timeframe != "1d":
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Legacy panel requires daily bars")
    if price_view is not None and query.price_basis != "raw":
        raise TiveloError(ErrorCode.RAW_PRICE_REQUIRED, "Fixed projection requires raw bars")
    if field_cutoff_time is not None and field_cutoff_time.utcoffset() is None:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Field cutoff time needs a UTC offset")
    names = {
        instrument: instrument if symbols is None else symbols.get(instrument)
        for instrument in query.instrument_ids
    }
    if not all(names.values()) or len(set(names.values())) != len(names):
        raise TiveloError(
            ErrorCode.AMBIGUOUS_INSTRUMENT, "Daily panel requires unique symbol bindings"
        )

    source = (
        reader.iter_bars(query) if type(reader) is PortableSnapshotReader else reader.bars(query)
    )
    original = _checked_daily_bars(source, query, names)
    schema = dict(_PANEL_SCHEMA)
    raw_columns = ()
    if price_view is not None:
        bars = tuple(original)
        projector = StaticPriceView(price_view.mode)
        factors = (
            reader.factors(
                HistoryQuery(
                    instrument_ids=query.instrument_ids,
                    effective_at=query.as_of,
                    as_of=query.as_of,
                )
            )
            if price_view.mode != "raw" and bars
            else ()
        )
        prices = projector.iter_prices(bars, factors, price_view, decision_at=query.as_of)
        pairs = zip(bars, prices, strict=True)
        raw_columns = tuple(f"raw_{name}" for name in _PRICE_COLUMNS)
        schema.update(dict.fromkeys(raw_columns, pl.Float64))
    else:
        pairs = ((bar, None) for bar in original)

    lot_context = Context(prec=38)
    raw_fields = tuple(zip(_PRICE_COLUMNS, raw_columns, strict=False))
    columns = {name: [] for name in schema}
    groups = defaultdict(list) if extension_fields or include_st or include_names else None
    for index, (bar, price) in enumerate(pairs):
        if groups is not None:
            cutoff = (
                bar.bar_end if field_cutoff_time is None
                else datetime.combine(bar.trading_date, field_cutoff_time)
            )
            if not bar.bar_start <= cutoff <= bar.bar_end:
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Field cutoff is outside the daily bar")
            groups[cutoff].append((index, bar.instrument_id))
        columns["symbol"].append(names[bar.instrument_id])
        columns["date"].append(bar.trading_date)
        for name in _PRICE_COLUMNS:
            value = getattr(bar, name) if price is None else price[name]
            columns[name].append(float(value))
        columns["volume"].append(float(bar.volume.scaleb(-2, context=lot_context)))
        columns["amount"].append(None if bar.turnover is None else float(bar.turnover))
        for name, raw_column in raw_fields:
            columns[raw_column].append(float(getattr(bar, name)))
    panel = pl.DataFrame(columns, schema=schema)
    if query.price_basis == "raw" and price_view is None:
        panel = panel.with_columns(pl.col(name).alias(f"raw_{name}") for name in _PRICE_COLUMNS)
    if groups is not None:
        fields = extension_fields or {}
        if fields.keys() & _RESERVED_FIELDS:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Extension field shadows a daily column")
        panel = panel.with_columns(
            _daily_fields(reader, groups, panel.height, fields, include_st, include_names)
        )
        if "float_shares" in fields and "turnover_rate" not in fields:
            panel = panel.with_columns(
                pl.when(pl.col("float_shares") > 0)
                .then(pl.col("volume") * 10_000.0 / pl.col("float_shares"))
                .otherwise(None)
                .alias("turnover_rate")
            )
    return panel.sort("symbol", "date")
