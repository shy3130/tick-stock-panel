"""Mapping helpers for custom data sources."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import polars as pl


def extract_rows(payload: Any, response_path: str = "") -> list[dict]:
    """Extract a list of row dicts from a JSON payload using dot-path lookup."""
    data = payload
    if response_path:
        for part in response_path.split("."):
            if not part:
                continue
            if isinstance(data, dict):
                data = data.get(part)
            else:
                data = None
                break
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def map_rows(rows: list[dict], field_map: dict[str, str]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(rows)
    rename = {src: dst for src, dst in field_map.items() if src in df.columns and src != dst}
    if rename:
        df = df.rename(rename)
    keep = list(dict.fromkeys(field_map.values()))
    keep = [col for col in keep if col in df.columns]
    return df.select(keep) if keep else pl.DataFrame()


def apply_transforms(df: pl.DataFrame, transforms: dict[str, str]) -> pl.DataFrame:
    """Apply a small safe transform set. No eval is used."""
    if df.is_empty() or not transforms:
        return df
    out = df
    for col, expr in transforms.items():
        if col not in out.columns:
            continue
        text = expr.strip()
        if text == "value * 100":
            out = out.with_columns((pl.col(col).cast(pl.Float64, strict=False) * 100).alias(col))
        elif text == "value / 100":
            out = out.with_columns((pl.col(col).cast(pl.Float64, strict=False) / 100).alias(col))
        elif text == "value / 10000":
            out = out.with_columns((pl.col(col).cast(pl.Float64, strict=False) / 10000).alias(col))
        elif text.startswith("parse_date("):
            fmt = _extract_format(text) or "%Y-%m-%d"
            out = out.with_columns(
                pl.col(col).cast(pl.Utf8, strict=False).str.strptime(pl.Date, format=fmt, strict=False).alias(col)
            )
        elif text.startswith("parse_datetime("):
            fmt = _extract_format(text) or "%Y-%m-%d %H:%M:%S"
            out = out.with_columns(
                pl.col(col).cast(pl.Utf8, strict=False).str.strptime(pl.Datetime, format=fmt, strict=False).alias(col)
            )
    return out


def _extract_format(expr: str) -> str | None:
    for quote in ("'", '"'):
        if quote in expr:
            parts = expr.split(quote)
            if len(parts) >= 3:
                return parts[1]
    return None


def datetime_payload(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
