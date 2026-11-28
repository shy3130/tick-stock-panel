"""Read the locked stock minute files once into a bounded, sortable scratch store."""

import stat
from datetime import date, timedelta
from glob import escape

import pyarrow as pa
import pyarrow.parquet as pq
from tivelo_contracts.errors import ErrorCode, TiveloError

from ..adapters.cn_daily import CN
from .minute_prices import marker_path
from .repository_files import (
    DAILY_TABLES,
    MINUTE_TABLE,
    changed,
    copy_verified,
    date_partition_files,
    identity,
)

PRICES = ("open", "high", "low", "close")
VALUES = (*PRICES, "volume", "amount")


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid v1 minute archive file")
    return identity(info)


def inventory(root, start, end, symbols):
    minute = root / MINUTE_TABLE
    if minute.is_symlink() or not minute.is_dir():
        raise TiveloError(ErrorCode.SOURCE_UNAVAILABLE, "Stock minute archive is unavailable")
    marker = marker_path(root)
    try:
        marker_id = _regular(marker)
    except FileNotFoundError:
        marker_id = None
    minutes = date_partition_files(minute, start, end)
    # v1 also supports the old per-symbol layout, including files with no symbol column.
    for symbol in symbols:
        folder = minute / ("symbol=" + symbol)
        if folder.is_symlink():
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid v1 symbol partition")
        if folder.is_dir():
            minutes.update({path: _regular(path) for path in folder.glob("*.parquet")})
    daily = root / DAILY_TABLES["daily_raw_v1"]
    anchors = {}
    if marker_id is None and (daily.exists() or daily.is_symlink()):
        anchors = date_partition_files(daily, start, end)
    return marker_id, minutes, anchors


def _numeric(field):
    return pa.types.is_floating(field) or pa.types.is_integer(field) or pa.types.is_decimal(field)


def _numeric_text(schema, name):
    if name not in schema.names:
        return "NULL"
    # v1 normalizes floating values to Float64 before taking their shortest spelling.
    # In particular a stored Float32 must not silently become a shorter decimal.
    column = f'"{name}"'
    if pa.types.is_floating(schema.field(name).type):
        column += "::DOUBLE"
    return column + "::VARCHAR"


def _schema(schema, *, minute, symbol_folder):
    required = {"datetime", *PRICES, "volume"} if minute else {"close"}
    if not symbol_folder:
        required.add("symbol")
    if not required <= set(schema.names) or len(set(schema.names)) != len(schema.names):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Missing or ambiguous archive columns")
    if "symbol" in schema.names:
        symbol = schema.field("symbol").type
        if not (pa.types.is_string(symbol) or pa.types.is_large_string(symbol)):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid archive symbol field")
    values = VALUES if minute else ("close",)
    for name in values:
        if name in schema.names:
            field = schema.field(name).type
            if not _numeric(field) and not (name == "amount" and pa.types.is_null(field)):
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid archive financial column")
    if minute:
        field = schema.field("datetime").type
        if not pa.types.is_timestamp(field) or field.tz is not None:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Expected naive Beijing minute timestamps"
            )
    elif ("date" in schema.names and not pa.types.is_date(schema.field("date").type)) or (
        "quote_ts" in schema.names
        and not (
            pa.types.is_integer(schema.field("quote_ts").type)
            or pa.types.is_null(schema.field("quote_ts").type)
        )
    ):
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid raw daily close evidence")


def stage_archives(connection, directory, archive, context):
    connection.execute(
        "CREATE TABLE minutes(symbol VARCHAR, stamp TIMESTAMP, priority INTEGER, o VARCHAR, h VARCHAR, l VARCHAR, c VARCHAR, v VARCHAR, a VARCHAR)"
    )
    connection.execute(
        "CREATE TABLE anchors(day DATE, symbol VARCHAR, raw_close VARCHAR, quote_ts BIGINT)"
    )
    frozen = directory / "verified.parquet"
    symbols = list(archive.bindings)
    offset = timedelta(minutes=1) if archive.source.minute_label == "end" else timedelta()
    request = archive.source.request
    lower, upper = (
        boundary.astimezone(CN).replace(tzinfo=None) + offset
        for boundary in (request.start, request.end)
    )

    def freeze(path):
        context.check_cancelled()
        try:
            with frozen.open("wb") as stream:
                copy_verified(path, stream, archive.hashes[path], context)
        except FileNotFoundError:
            raise changed() from None
        return pq.read_schema(frozen)

    for path in sorted(archive.minutes):
        folder = path.parent.name
        symbol_folder = folder.startswith("symbol=")
        schema = freeze(path)
        _schema(schema, minute=True, symbol_folder=symbol_folder)
        field = "symbol" if "symbol" in schema.names else "NULL::VARCHAR"
        symbol = folder[7:] if symbol_folder else None
        day = None if symbol_folder else date.fromisoformat(folder[5:])
        # Only internally selected column names enter SQL. All filesystem paths,
        # symbols and dates remain bound values. Text keeps exact integer/decimal
        # inputs through staging alongside v1 Float64 shortest decimal spellings.
        values = ", ".join(_numeric_text(schema, k) for k in VALUES)
        connection.execute(
            f"""INSERT INTO minutes
                SELECT CASE WHEN ? IS NOT NULL AND {field} <> ? THEN error('symbol partition mismatch')
                            ELSE coalesce({field}, ?) END,
                       CASE WHEN datetime IS NULL OR epoch_ns(datetime) % 60000000000 <> 0
                                  OR (?::DATE IS NOT NULL AND datetime::DATE <> ?)
                            THEN error('invalid minute label') ELSE datetime END, ?, {values}
                FROM read_parquet(?, hive_partitioning=false)
                WHERE (? OR {field} IN (SELECT unnest(?)))
                  AND (?::DATE IS NOT NULL OR datetime IS NULL OR datetime::DATE BETWEEN ? AND ?)
                  AND (? OR datetime IS NULL OR (datetime >= ? AND datetime < ?)
                         OR (?::DATE IS NOT NULL AND datetime::DATE <> ?))""",
            [
                symbol,
                symbol,
                symbol,
                day,
                day,
                int(symbol_folder),
                escape(frozen.as_posix()),
                symbol_folder,
                symbols,
                day,
                archive.first,
                archive.last,
                archive.marker is None,
                lower,
                upper,
                day,
                day,
            ],
        )
    for path in sorted(archive.anchors):
        schema = freeze(path)
        _schema(schema, minute=False, symbol_folder=False)
        day = date.fromisoformat(path.parent.name[5:])
        trading_date = "date" if "date" in schema.names else "?::DATE"
        stamp = "quote_ts" if "quote_ts" in schema.names else "NULL"
        args = [] if "date" in schema.names else [day]
        connection.execute(
            f"""INSERT INTO anchors
                SELECT CASE WHEN {trading_date} IS DISTINCT FROM ?::DATE THEN error('daily date mismatch')
                            ELSE ?::DATE END, symbol, {_numeric_text(schema, "close")}, {stamp}
                FROM read_parquet(?, hive_partitioning=false)
                WHERE symbol IN (SELECT unnest(?))""",
            [*args, day, day, escape(frozen.as_posix()), symbols],
        )
    frozen.unlink(missing_ok=True)
