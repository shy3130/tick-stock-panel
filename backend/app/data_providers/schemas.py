"""Internal provider schema column lists."""
from __future__ import annotations

DAILY_COLUMNS = [
    "symbol", "asset_type", "source", "date", "open", "high", "low", "close",
    "volume", "amount", "pre_close", "change_pct",
]

ADJ_FACTOR_COLUMNS = ["symbol", "asset_type", "source", "trade_date", "ex_factor"]

INSTRUMENT_COLUMNS = [
    "symbol", "name", "exchange", "asset_type", "source", "list_date", "status",
]

MINUTE_COLUMNS = [
    "symbol", "asset_type", "source", "datetime", "open", "high", "low", "close",
    "volume", "amount", "freq",
]
