"""Provider contracts for external market data sources."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

import polars as pl

AssetType = Literal["stock", "index", "etf", "hk"]


@dataclass(frozen=True)
class ProviderCapabilities:
    instruments: bool = False
    daily: bool = False
    adj_factor: bool = False
    minute: bool = False
    realtime: bool = False
    financial: bool = False
    depth: bool = False
    # universes: 指数 / 板块 / 行业集合（按 universe 名返回标的清单）
    universes: bool = False
    # Whether provider explicitly allows multi-month minute-history extension.
    minute_month_extension: bool = False
    # Published ordered-trans research artifacts; raw CSV is never runtime input.
    ordered_trans_research: bool = False


class OrderedTransReaderFactory(Protocol):
    """Provider factory for a request-owned pinned ordered-trans reader."""

    def open_ordered_trans_reader(self) -> object | None:
        ...


class MarketDataProvider(OrderedTransReaderFactory, Protocol):
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
    ) -> pl.DataFrame:
        """Return normalized minute K rows. Implementations may return empty."""

    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> pl.DataFrame:
        """Return normalized realtime quotes. Implementations may return empty."""

    def get_depth(self, symbols: list[str]) -> dict:
        """Return depth rows as {symbol: {bid/ask_prices, bid/ask_volumes, timestamp}}."""
        return {}

    def get_by_universes(
        self,
        universes: list[str],
        asset_type: AssetType = "index",  # noqa: ARG003
    ) -> pl.DataFrame:
        """Return normalized instrument rows for given universes.

        输出列（与 INSTRUMENT_COLS 对齐）：symbol / name / code / exchange /
        asset_type / source。``asset_type`` 参数用于驱动不同 universe 的归一
        映射（如 ``"index"`` / ``"etf"`` / ``"sector"``），provider 可按需扩展。

        默认实现返回空 df；具体 provider 应重写以从自身数据源（如 fstore
        ``chengfen_gu``）取数。
        契约目的：让 service 层（典型为 ``index_sync.sync_index_instruments``
        的"付费补充"逻辑）摆脱直接 SDK 调用。
        """
        return pl.DataFrame()
