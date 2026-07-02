"""Normalize provider responses into internal Polars schemas."""
from __future__ import annotations

import polars as pl

from app.indicators.pipeline import filter_halt_days

DAILY_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
ADJ_FACTOR_COLS = ["symbol", "trade_date", "ex_factor"]
INSTRUMENT_COLS = ["symbol", "name", "code", "exchange", "asset_type", "source"]
REALTIME_COLS = [
    "symbol", "name", "last_price", "prev_close", "open", "high", "low",
    "volume", "amount", "timestamp", "source", "ext",
]


def to_polars(data) -> pl.DataFrame:
    if data is None:
        return pl.DataFrame()
    if isinstance(data, pl.DataFrame):
        return data
    if isinstance(data, dict):
        rows: list[dict] = []
        for sym, values in data.items():
            for item in values or []:
                row = dict(item or {})
                row.setdefault("symbol", sym)
                rows.append(row)
        return pl.DataFrame(rows) if rows else pl.DataFrame()
    if hasattr(data, "reset_index"):
        return pl.from_pandas(data.reset_index())
    try:
        return pl.DataFrame(data)
    except Exception:  # noqa: BLE001
        return pl.DataFrame()


def normalize_daily(data, default_symbol: str | None = None, source: str = "tickflow") -> pl.DataFrame:  # noqa: ARG001
    df = to_polars(data)
    if df.is_empty():
        return df
    rename_map = {
        "ts_code": "symbol",
        "trade_date": "date",
        "datetime": "date",
        "vol": "volume",
        "amt": "amount",
    }
    df = df.rename({k: v for k, v in rename_map.items() if k in df.columns})
    if "symbol" not in df.columns and default_symbol:
        df = df.with_columns(pl.lit(default_symbol).alias("symbol"))
    if "date" in df.columns and df.schema["date"] != pl.Date:
        df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
    df = filter_halt_days(df)
    keep = [c for c in DAILY_COLS if c in df.columns]
    return df.select(keep) if keep else pl.DataFrame()


def normalize_adj_factors(data, source: str = "tickflow") -> pl.DataFrame:  # noqa: ARG001
    df = to_polars(data)
    if df.is_empty():
        return df
    rename_map = {
        "timestamp": "trade_date",
        "date": "trade_date",
        "adj_factor": "ex_factor",
    }
    df = df.rename({k: v for k, v in rename_map.items() if k in df.columns})
    if "trade_date" in df.columns:
        if df.schema["trade_date"] in {pl.Int64, pl.Int32, pl.UInt64, pl.UInt32, pl.Float64, pl.Float32}:
            df = df.with_columns(
                pl.from_epoch(pl.col("trade_date").cast(pl.Int64), time_unit="ms").dt.date().alias("trade_date")
            )
        else:
            df = df.with_columns(pl.col("trade_date").cast(pl.Date, strict=False))
    if "ex_factor" in df.columns:
        df = df.with_columns(pl.col("ex_factor").cast(pl.Float64, strict=False))
    keep = [c for c in ADJ_FACTOR_COLS if c in df.columns]
    return df.select(keep).drop_nulls() if len(keep) == len(ADJ_FACTOR_COLS) else pl.DataFrame()


def normalize_instruments(rows: list[dict], asset_type: str, source: str = "tickflow") -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    out: list[dict] = []
    for item in rows:
        symbol = item.get("symbol")
        if not symbol:
            continue
        out.append({
            "symbol": str(symbol),
            "name": item.get("name") or str(symbol),
            "code": item.get("code") or str(symbol).split(".")[0],
            "exchange": item.get("exchange"),
            "asset_type": asset_type,
            "source": source,
        })
    if not out:
        return pl.DataFrame()
    return pl.DataFrame(out).select(INSTRUMENT_COLS).unique(subset=["symbol"], keep="last").sort("symbol")


def normalize_realtime(data, source: str = "tickflow") -> pl.DataFrame:
    """Normalize realtime quote rows to quote_service's provider contract."""
    df = to_polars(data)
    if df.is_empty():
        return df

    rename_map = {
        "price": "last_price",
        "close": "last_price",
        "pre_close": "prev_close",
        "last_close": "prev_close",
        "datetime": "timestamp",
    }
    df = df.rename({k: v for k, v in rename_map.items() if k in df.columns and v not in df.columns})

    if "symbol" not in df.columns:
        return pl.DataFrame()
    df = df.with_columns(pl.col("symbol").cast(pl.Utf8).str.to_uppercase())

    for col in ("last_price", "prev_close", "open", "high", "low", "volume", "amount"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))

    if "source" not in df.columns:
        df = df.with_columns(pl.lit(source).alias("source"))
    if "timestamp" not in df.columns:
        df = df.with_columns(pl.lit(None).cast(pl.Utf8).alias("timestamp"))
    else:
        df = df.with_columns(pl.col("timestamp").cast(pl.Utf8, strict=False))
    if "name" not in df.columns:
        df = df.with_columns(pl.lit(None).cast(pl.Utf8).alias("name"))

    if "ext" not in df.columns:
        ext_fields = []
        for col in ("change_pct", "change_amount", "amplitude", "turnover_rate"):
            if col in df.columns:
                ext_fields.append(pl.col(col).alias(col))
            else:
                ext_fields.append(pl.lit(None).alias(col))
        ext_fields.extend([
            pl.col("name").alias("name"),
            pl.col("source").alias("source"),
        ])
        df = df.with_columns(pl.struct(ext_fields).alias("ext"))

    for col in REALTIME_COLS:
        if col not in df.columns:
            df = df.with_columns(pl.lit(None).alias(col))
    return df.select(REALTIME_COLS).drop_nulls(subset=["symbol", "last_price"])
