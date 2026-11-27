"""Polars parquet helpers."""
from __future__ import annotations

from contextlib import suppress
from glob import escape
from os.path import samestat
from pathlib import Path, PurePath
from typing import Any
from unicodedata import normalize

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


def parquet_glob(directory: PurePath, pattern: str = "**/*.parquet") -> str:
    """Match a Parquet pattern beneath a literal directory on Windows and POSIX."""
    if isinstance(directory, Path):
        # Escaped components are still matched case-sensitively by the globber.
        parts = list(directory.parts)
        for index, part in enumerate(parts):
            if part == directory.anchor or escape(part) == part:
                continue
            parent = Path(*parts[:index])
            with suppress(OSError):
                expected = (parent / part).lstat()
                folded = normalize("NFC", part).casefold()
                for entry in parent.iterdir():
                    if entry.name == part:
                        break
                    if normalize("NFC", entry.name).casefold() == folded and samestat(
                        entry.lstat(), expected
                    ):
                        parts[index] = entry.name
                        break
        directory = Path(*parts)
    return f"{escape(directory.as_posix().rstrip('/'))}/{pattern}"


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
