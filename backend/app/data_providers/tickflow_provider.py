"""TickFlow provider implementation."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities
from app.data_providers.normalizer import normalize_adj_factors, normalize_daily, normalize_instruments
from app.tickflow.client import get_client

logger = logging.getLogger(__name__)

_EXCHANGES = ["SH", "SZ", "BJ"]


class TickFlowProvider:
    name = "tickflow"
    capabilities = ProviderCapabilities(
        instruments=True,
        daily=True,
        adj_factor=True,
        minute=True,
        realtime=True,
        depth5=True,
        financial=True,
    )

    def get_instruments(self, asset_type: AssetType) -> pl.DataFrame:
        tf = get_client()
        instrument_type = "stock" if asset_type == "stock" else asset_type
        rows: list[dict] = []
        for ex in _EXCHANGES:
            try:
                items = tf.exchanges.get_instruments(ex, instrument_type=instrument_type)
                rows.extend([it for it in (items or []) if isinstance(it, dict)])
            except Exception as e:  # noqa: BLE001
                logger.warning("TickFlow instruments %s/%s failed: %s", ex, instrument_type, e)
        return normalize_instruments(rows, asset_type=asset_type, source=self.name)

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType,  # noqa: ARG002
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        tf = get_client()
        kwargs = {
            "period": "1d",
            "adjust": "none",
            "count": 10000 if start_time and end_time else 250,
            "as_dataframe": False,
            "show_progress": False,
        }
        if start_time and end_time:
            from app.services.kline_sync import _compact_klines_to_df, _datetime_to_ms, _timestamp_to_beijing_datetime
            kwargs["start_time"] = _datetime_to_ms(start_time)
            kwargs["end_time"] = _datetime_to_ms(end_time)
        else:
            from app.services.kline_sync import _compact_klines_to_df, _timestamp_to_beijing_datetime
        raw = tf.klines.batch(symbols, **kwargs)
        # False 直转: 列数组→polars (无 pandas 中转), 加北京墙钟 datetime 列
        # (normalize_daily 映射为 date); 保留 timestamp 原列 — normalize_daily
        # 会把它改名为 quote_ts (盘后校验/量比折算用), 不能像 kline_sync 路径那样丢弃。
        seg = _compact_klines_to_df(raw)
        if seg.is_empty():
            return pl.DataFrame()
        seg = seg.with_columns(_timestamp_to_beijing_datetime(pl.col("timestamp")).alias("datetime"))
        return normalize_daily(seg, source=self.name)

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType,  # noqa: ARG002
    ) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        tf = get_client()
        kwargs = {"as_dataframe": False}
        if start_time or end_time:
            from app.services.kline_sync import _datetime_to_ms
            if start_time:
                kwargs["start_time"] = _datetime_to_ms(start_time)
            if end_time:
                kwargs["end_time"] = _datetime_to_ms(end_time)
        raw = tf.klines.ex_factors(symbols, **kwargs)
        return normalize_adj_factors(raw, source=self.name)

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",  # noqa: ARG002
        freq: str = "1m",  # noqa: ARG002
        on_chunk_done: Callable[[int, int], None] | None = None,  # noqa: ARG002
    ) -> pl.DataFrame:
        # Existing minute sync remains in app.services.kline_sync for now.
        return pl.DataFrame()

    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> pl.DataFrame:
        tf = get_client()
        if universes and symbols:
            raise ValueError("TickFlow realtime accepts either universes or symbols, not both")
        if universes:
            resp = tf.quotes.get_by_universes(universes=universes)
        elif symbols:
            resp = tf.quotes.get(symbols=symbols)
        else:
            return pl.DataFrame()
        return pl.DataFrame(resp or [])

    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        data = get_client().depth.batch(symbols)
        return data if isinstance(data, dict) else {}
