"""三市场作用域的统一契约。"""
from __future__ import annotations

from datetime import date
from typing import Literal

import polars as pl

MarketCode = Literal["cn", "hk", "us"]

_CURRENCY_BY_MARKET: dict[MarketCode, str] = {
    "cn": "CNY",
    "hk": "HKD",
    "us": "USD",
}


def normalize_market(value: str | None) -> MarketCode:
    normalized = str(value or "").strip().lower()
    if normalized in _CURRENCY_BY_MARKET:
        return normalized  # type: ignore[return-value]
    return "cn"


def market_currency(value: str | None) -> str:
    return _CURRENCY_BY_MARKET[normalize_market(value)]


def market_cache_key(value: str | None, as_of: date | None = None) -> str:
    market = normalize_market(value)
    date_key = as_of.isoformat() if as_of else "latest"
    return f"{market}:{date_key}"


def filter_frame_by_market(frame: pl.DataFrame, value: str | None) -> pl.DataFrame:
    """按标准证券后缀过滤，未知或缺少 symbol 时返回空集以避免跨市场泄漏。"""
    if frame.is_empty():
        return frame
    if "symbol" not in frame.columns:
        return frame.clear()

    symbol = pl.col("symbol").cast(pl.Utf8).str.to_uppercase()
    market = normalize_market(value)
    if market == "hk":
        predicate = symbol.str.ends_with(".HK")
    elif market == "us":
        predicate = symbol.str.ends_with(".US")
    else:
        predicate = (
            symbol.str.ends_with(".SH")
            | symbol.str.ends_with(".SZ")
            | symbol.str.ends_with(".BJ")
        )
    return frame.filter(predicate)
