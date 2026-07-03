from __future__ import annotations

import unicodedata
from functools import lru_cache

import polars as pl
from pypinyin import Style, lazy_pinyin


def normalize_name_for_pinyin(name: str) -> str:
    return "".join(unicodedata.normalize("NFKC", name or "").split())


@lru_cache(maxsize=16_384)
def pinyin_keys(name: str) -> tuple[str, str]:
    normalized = normalize_name_for_pinyin(name)
    if not normalized:
        return "", ""
    full = "".join(lazy_pinyin(normalized, errors="ignore")).lower()
    initials = "".join(lazy_pinyin(normalized, style=Style.FIRST_LETTER, errors="ignore")).lower()
    return full, initials


def add_pinyin_columns(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty() or "name" not in df.columns:
        return df
    return df.with_columns(
        pl.col("name")
        .map_elements(lambda value: pinyin_keys(str(value or ""))[0], return_dtype=pl.Utf8)
        .alias("name_pinyin"),
        pl.col("name")
        .map_elements(lambda value: pinyin_keys(str(value or ""))[1], return_dtype=pl.Utf8)
        .alias("name_initials"),
    )
