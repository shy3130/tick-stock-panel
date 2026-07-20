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
            "as_dataframe": True,
            "show_progress": False,
        }
        if start_time and end_time:
            from app.services.kline_sync import _datetime_to_ms
            kwargs["start_time"] = _datetime_to_ms(start_time)
            kwargs["end_time"] = _datetime_to_ms(end_time)
        raw = tf.klines.batch(symbols, **kwargs)
        frames: list[pl.DataFrame] = []
        if isinstance(raw, dict):
            for sym, sub in raw.items():
                normalized = normalize_daily(sub, default_symbol=sym, source=self.name)
                if not normalized.is_empty():
                    frames.append(normalized)
        else:
            normalized = normalize_daily(raw, source=self.name)
            if not normalized.is_empty():
                frames.append(normalized)
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

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
