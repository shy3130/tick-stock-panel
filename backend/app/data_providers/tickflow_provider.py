"""TickFlow provider implementation."""
from __future__ import annotations

import logging
from datetime import datetime

import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities
from app.data_providers.normalizer import (
    normalize_adj_factors,
    normalize_daily,
    normalize_instruments,
    normalize_realtime,
)
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
        depth=True,
        universes=True,
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
        asset_type: AssetType,  # noqa: ARG002
        freq: str = "1m",  # noqa: ARG002
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
        return normalize_realtime(resp or [], source=self.name)

    # ------------------------------------------------------------------ #
    # get_by_universes — 阶段 3 #3.2 universes 索引标的能力
    # ------------------------------------------------------------------ #
    def get_by_universes(
        self,
        universes: list[str],
        asset_type: AssetType = "index",  # noqa: ARG002
    ) -> pl.DataFrame:
        """TickFlow 端 ``get_by_universes`` —— 阶段 3 #3.2 行为对齐。

        保留对 ``tf.quotes.get_by_universes`` 的直接调用；与 ``index_sync.py``
        原本的"补充指数列表"语义一致（返回该 universe 下的标的列表）。

        输出列：INSTRUMENT_COLS（symbol / name / code / exchange / asset_type /
        source）。``asset_type`` 由调用方决定（``index`` 用于 index_sync）。

        降级：SDK 调用失败 → 返回空 df，warning。
        """
        if not universes:
            return pl.DataFrame()
        try:
            tf = get_client()
            resp = tf.quotes.get_by_universes(universes=universes, as_dataframe=False)
        except Exception as e:  # noqa: BLE001
            logger.warning("TickFlow get_by_universes(%s) failed: %s", universes, e)
            return pl.DataFrame()
        if not resp:
            return pl.DataFrame()
        # resp 是 list[dict] / pandas.DataFrame
        if hasattr(resp, "reset_index") and not isinstance(resp, list):
            try:
                df = pl.from_pandas(resp.reset_index())
            except Exception:  # noqa: BLE001
                df = pl.DataFrame(list(resp) if resp else [])
        elif isinstance(resp, pl.DataFrame):
            df = resp
        else:
            rows: list[dict] = []
            for q in resp:
                item = q if isinstance(q, dict) else {}
                ext = item.get("ext") or {}
                symbol = item.get("symbol")
                if not symbol:
                    continue
                rows.append({
                    "symbol": str(symbol),
                    "name": ext.get("name") or item.get("name") or str(symbol),
                })
            df = pl.DataFrame(rows) if rows else pl.DataFrame()

        if df.is_empty() or "symbol" not in df.columns:
            return pl.DataFrame()
        # rename ts_code -> symbol 兼容
        df = df.rename({k: v for k, v in {"ts_code": "symbol"}.items() if k in df.columns})
        if "name" not in df.columns:
            df = df.with_columns(pl.col("symbol").cast(pl.Utf8).alias("name"))
        out = df.select([pl.col("symbol").cast(pl.Utf8), pl.col("name").cast(pl.Utf8)]).with_columns([
            pl.col("symbol").str.split(".").list.first().alias("code"),
            pl.lit(str(asset_type)).alias("asset_type"),
        ])
        # exchange 留空字符串（TickFlow 不暴露），source 标注
        out = out.with_columns([
            pl.lit("").alias("exchange"),
            pl.lit(self.name).alias("source"),
        ])
        return out.unique(subset=["symbol"], keep="last").sort("symbol")
