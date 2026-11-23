"""分钟源的流式兼容与请求节流;不在业务同步层拆分标的。"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import polars as pl
from tivelo_data.legacy.pacing import RequestPacer as RequestPacer

from app.data_providers.base import AssetType
from app.data_providers.minute_normalizer import _normalize_minute


class LegacyMinuteAdapter:
    """旧 get_minute 的流式协议适配;不拆分或改写源收到的请求。"""

    def __init__(self, provider) -> None:
        self._provider = provider

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def iter_minute(
        self, symbols: list[str], start_time: datetime | None, end_time: datetime | None,
        asset_type: AssetType = "stock", freq: str = "1m", on_chunk_done=None,
    ) -> Iterator[pl.DataFrame]:
        frame = self._provider.get_minute(
            symbols, start_time=start_time, end_time=end_time, asset_type=asset_type,
            freq=freq, on_chunk_done=on_chunk_done,
        )
        if not isinstance(frame, pl.DataFrame):
            # 旧入口允许 None 空结果或 pandas 帧,保留原归一行为。
            frame = _normalize_minute(frame, source=getattr(self._provider, "name", "legacy"))
        yield frame


def with_minute_stream(provider):
    """为缺少流式入口的旧分钟源补充适配,不改变其他能力和属性。"""
    if callable(getattr(provider, "iter_minute", None)):
        return provider
    if callable(getattr(provider, "get_minute", None)):
        return LegacyMinuteAdapter(provider)
    return provider


def iter_minute_frames(provider, symbols, **kwargs) -> Iterator[pl.DataFrame]:
    """转发源的完整迭代会话;显式关闭以释放 ManagedProvider 的在途租约。"""
    source = with_minute_stream(provider)
    stream = source.iter_minute(symbols, **kwargs)
    try:
        yield from stream
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
