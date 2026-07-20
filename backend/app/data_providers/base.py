"""Provider contracts for external market data sources.

The first implementation wraps TickFlow. Other providers (Tushare/AkShare/etc.)
should return the same normalized Polars schemas so storage, indicators and
backtests stay data-source agnostic.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

import polars as pl

AssetType = Literal["stock", "index", "etf"]


@dataclass(frozen=True)
class ProviderCapabilities:
    instruments: bool = False
    daily: bool = False
    adj_factor: bool = False
    minute: bool = False
    realtime: bool = False
    financial: bool = False


class MarketDataProvider(Protocol):
    name: str
    capabilities: ProviderCapabilities

    def get_instruments(self, asset_type: AssetType) -> pl.DataFrame:
        """Return normalized instruments: symbol/name/code/exchange/asset_type/source."""

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType,
    ) -> pl.DataFrame:
        """Return normalized daily K rows."""

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType,
    ) -> pl.DataFrame:
        """Return normalized adjustment factors: symbol/trade_date/ex_factor."""

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType,
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """Return normalized minute K rows. Implementations may return empty.

        on_chunk_done 契约: provider 实现内部以 2 参 (cur, total) 调用;
        3 参 seg_label 适配由 kline_sync._try_custom_minute 包装层负责,
        不应泄漏到 provider 契约层。
        """

    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> pl.DataFrame:
        """Return normalized realtime quotes. Implementations may return empty."""
