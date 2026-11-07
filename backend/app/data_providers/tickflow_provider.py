"""TickFlow provider implementation."""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities, ProviderRequestError
from app.data_providers.minute_normalizer import (
    _compact_klines_to_df,
    _datetime_to_ms,
    _normalize_minute,
)
from app.data_providers.minute_stream import RequestPacer
from app.data_providers.normalizer import (
    normalize_adj_factors,
    normalize_daily,
    normalize_instruments,
)
from app.market_time import CN_TZ
from app.tickflow.client import get_client
from app.tickflow.request_guard import _sdk_call_with_deadline

logger = logging.getLogger(__name__)

_EXCHANGES = ["SH", "SZ", "BJ"]
_MINUTE_PACER = RequestPacer()


@dataclass(frozen=True)
class TickFlowMinuteOptions:
    """旧同步入口的供应商选项,不扩散到第三方 Provider 契约。"""

    batch_size: int | None = None
    rpm: int | None = None
    count: int | None = None
    segment_trading_days: int = 20
    raw_basis: bool = False


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

    def __init__(self, *, minute_options: TickFlowMinuteOptions | None = None,
                 minute_client=None, minute_call=None, minute_progress=None):
        self._minute_options = minute_options or TickFlowMinuteOptions()
        self._minute_client = minute_client
        self._minute_call = minute_call or _sdk_call_with_deadline
        self._minute_progress = minute_progress

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
            from app.services.kline_sync import (
                _compact_klines_to_df,
                _datetime_to_ms,
                _timestamp_to_beijing_datetime,
            )
            kwargs["start_time"] = _datetime_to_ms(start_time)
            kwargs["end_time"] = _datetime_to_ms(end_time)
        else:
            from app.services.kline_sync import (
                _compact_klines_to_df,
                _timestamp_to_beijing_datetime,
            )
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
        asset_type: AssetType = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        parts = [df for df in self.iter_minute(
            symbols, start_time, end_time, asset_type, freq, on_chunk_done,
        ) if not df.is_empty()]
        return pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame()

    def iter_minute(
        self, symbols: list[str], start_time: datetime | None, end_time: datetime | None,
        asset_type: AssetType = "stock", freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """源内控制 SDK 请求批量、时间窗、节流与超时;每次请求后产出一帧。"""
        if not symbols or freq != "1m":
            return
        options = self._minute_options
        size = options.batch_size or len(symbols)
        if size <= 0:
            raise ProviderRequestError("TickFlow 分钟批量大小必须大于0")
        start = start_time if start_time is None or start_time.tzinfo else start_time.replace(tzinfo=CN_TZ)
        end = end_time if end_time is None or end_time.tzinfo else end_time.replace(tzinfo=CN_TZ)
        segments = []
        if start is not None and end is not None:
            span = timedelta(days=max(1, int(options.segment_trading_days * 7 / 5)))
            while start < end:
                stop = min(start + span, end)
                segments.append((start, stop))
                start = stop
        else:
            segments = [(None, None)]
        total = len(segments) * ((len(symbols) + size - 1) // size)
        client = self._minute_client if self._minute_client is not None else get_client()
        done = failures = 0
        for first, last in segments:
            label = f"{first:%m-%d}~{last:%m-%d}" if first and last else "最新"

            def report(cur, segment=label):
                if on_chunk_done:
                    on_chunk_done(cur, total)
                if self._minute_progress:
                    self._minute_progress(cur, total, segment)

            for offset in range(0, len(symbols), size):
                batch = symbols[offset:offset + size]
                _MINUTE_PACER.wait("history", options.rpm, lambda cur=done: report(cur))
                kwargs = {"period": "1m", "count": options.count or 1200,
                          "adjust": "none" if options.raw_basis else "forward",
                          "as_dataframe": False, "show_progress": False}
                if first is not None and last is not None:
                    kwargs.update(start_time=_datetime_to_ms(first), end_time=_datetime_to_ms(last), count=10000)
                done += 1
                try:
                    raw = self._minute_call(lambda wanted=batch, params=kwargs: client.klines.batch(wanted, **params))
                    frame = _normalize_minute(_compact_klines_to_df(raw))
                except Exception as exc:  # noqa: BLE001 -- every SDK failure leaves the job incomplete
                    logger.warning("TickFlow 分钟请求失败(%d只): %s", len(batch), type(exc).__name__)
                    failures += 1
                else:
                    if not frame.is_empty():
                        yield frame
                report(done)
        if failures:
            raise ProviderRequestError(f"TickFlow 分钟同步未完成: {failures}/{total} 批请求失败, 已保存的数据保留")

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
