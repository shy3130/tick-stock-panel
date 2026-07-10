"""Polars parquet helpers."""
from __future__ import annotations

from typing import Any

import polars as pl

DAILY_STORAGE_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "quote_ts": pl.Int64,
}

ENRICHED_STORAGE_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "raw_close": pl.Float64,
    "raw_high": pl.Float64,
    "raw_low": pl.Float64,
    "turnover_rate": pl.Float64,
    "consecutive_limit_ups": pl.UInt32,
    "consecutive_limit_downs": pl.UInt32,
    "quote_ts": pl.Int64,
}


def scan_parquet_compat(source: Any, **kwargs: Any) -> pl.LazyFrame:
    """Scan partitioned parquet while tolerating additive schema changes."""
    kwargs.setdefault("missing_columns", "insert")
    kwargs.setdefault("extra_columns", "ignore")
    return pl.scan_parquet(source, **kwargs)


def scan_daily_parquet(source: Any, **kwargs: Any) -> pl.LazyFrame:
    kwargs.setdefault("schema", DAILY_STORAGE_SCHEMA)
    kwargs.setdefault("cast_options", pl.ScanCastOptions(integer_cast="allow-float"))
    return scan_parquet_compat(source, **kwargs)


def scan_enriched_parquet(source: Any, **kwargs: Any) -> pl.LazyFrame:
    kwargs.setdefault("schema", ENRICHED_STORAGE_SCHEMA)
    kwargs.setdefault("cast_options", pl.ScanCastOptions(integer_cast="allow-float"))
    return scan_parquet_compat(source, **kwargs)
