"""Share overlapping minute gap pulls, retaining the existing provider/write contract."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import polars as pl

from app.services import kline_sync, minute_adjust
from app.services.shared_work import SharedWork, WorkBusyError

logger = logging.getLogger(__name__)
_work = SharedWork("minute.pull", max_pending=8192, max_entries=8192,
                   max_bytes=32 * 1024 * 1024, size_of=lambda frame: frame.estimated_size())


def cache_info() -> dict:
    return _work.cache_info()


def pull(repo, symbols: list[str], *, asset_type: str, start: datetime, end: datetime,
         batch_size: int, rpm: int, raw_basis: bool) -> pl.DataFrame:
    root = Path(repo.store.data_dir)
    provider = kline_sync._selected_minute_provider_name()
    folder = root / ("kline_etf_minute" if asset_type == "etf" else "kline_minute")
    partition = folder / f"date={start.date()}"
    local_version = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size)
                          for p in sorted(partition.glob("*.parquet")))
    prefix = (str(root.resolve()), provider, asset_type, raw_basis, start, end, local_version)
    keys = [(*prefix, symbol) for symbol in symbols]

    def fetch(owned):
        wanted = [key[-1] for key in owned]
        frame = kline_sync.sync_minute_batch(
            wanted, start_time=start, end_time=end, batch_size=batch_size, rpm=rpm,
            asset_type=asset_type, raw_basis=raw_basis,
        )
        if provider != kline_sync._selected_minute_provider_name() or minute_adjust.minute_basis_is_raw(root) != raw_basis:
            raise WorkBusyError("分钟数据源或价格基准已变化，请重新加载")
        if not frame.is_empty():
            try:
                with repo._write_lock:
                    kline_sync._write_minute_partition(frame, folder)
            except Exception:  # noqa: BLE001 -- persistence must preserve live/local fallback
                # Persistence remains an optimization: a write failure must not erase
                # valid rows received this round or the caller's local fallback.
                logger.warning("minute-batch 补拉落盘失败 (降级为仅返回)", exc_info=True)
        parts = {part["symbol"][0]: part for part in frame.partition_by("symbol")} if not frame.is_empty() else {}
        return {key: parts.get(key[-1], pl.DataFrame()) for key in owned}

    values = _work.run_many(keys, fetch, ttl_s=2)
    frames = [frame for frame in values.values() if not frame.is_empty()]
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
