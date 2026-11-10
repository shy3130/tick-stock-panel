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
_sync_work = SharedWork("minute.sync", max_pending=32, max_entries=128)


def sync_history(repo, capset, symbol: str, asset_type: str, days: int, *, on_updated=None) -> int:
    """跨窗口复用单标的历史补齐，执行体遵守共享容量和流式落盘契约。"""
    from app.market_time import cn_today
    from app.services.heavy_job_limiter import shared_heavy_job_limiter

    source = kline_sync.minute_source_stamp()
    key = (str(repo.store.data_dir.resolve()), capset, asset_type, symbol, days, cn_today(),
           source, minute_adjust.minute_basis_is_raw(repo.store.data_dir))

    def run():
        with shared_heavy_job_limiter.slot("normal", timeout=30):
            try:
                return kline_sync.sync_and_persist_minute(
                    [symbol], repo, capset, days=days, force_full_days=True, asset_type=asset_type,
                    expected_source=source,
                )
            finally:
                # 部分批次失败也可能已落盘，其他窗口应读取仓库中的有效结果。
                if on_updated:
                    on_updated("minute")

    return _sync_work.run(key, run, ttl_s=30)


def cache_info() -> dict:
    return _work.cache_info()


def pull(repo, symbols: list[str], *, asset_type: str, start: datetime, end: datetime,
         batch_size: int, rpm: int, raw_basis: bool, on_updated=None) -> pl.DataFrame:
    root = Path(repo.store.data_dir)
    source = kline_sync.minute_source_stamp()
    folder = root / ("kline_etf_minute" if asset_type == "etf" else "kline_minute")
    partition = folder / f"date={start.date()}"
    local_version = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size)
                          for p in sorted(partition.glob("*.parquet")))
    prefix = (str(root.resolve()), source, asset_type, raw_basis, start, end, local_version)
    keys = [(*prefix, symbol) for symbol in symbols]

    def fetch(owned):
        wanted = [key[-1] for key in owned]
        frame = kline_sync.sync_minute_batch(
            wanted, start_time=start, end_time=end, batch_size=batch_size, rpm=rpm,
            asset_type=asset_type, raw_basis=raw_basis,
        )
        persisted = False
        with repo._write_lock:
            if (source != kline_sync.minute_source_stamp()
                    or minute_adjust.minute_basis_is_raw(root) != raw_basis):
                raise WorkBusyError("分钟数据源或价格基准已变化，请重新加载")
            if not frame.is_empty():
                try:
                    kline_sync._write_minute_partition(frame, folder)
                    persisted = True
                except Exception:  # noqa: BLE001 -- callers can still use the valid live rows
                    logger.warning("minute-batch 补拉落盘失败 (降级为仅返回)", exc_info=True)
        if persisted and on_updated:
            try:
                on_updated("minute")
            except Exception:  # noqa: BLE001 -- notification failure must not discard persisted rows
                logger.warning("minute-batch 数据更新通知失败", exc_info=True)
        parts = {part["symbol"][0]: part for part in frame.partition_by("symbol")} if not frame.is_empty() else {}
        return {key: parts.get(key[-1], pl.DataFrame()) for key in owned}

    values = _work.run_many(keys, fetch, ttl_s=2)
    frames = [frame for frame in values.values() if not frame.is_empty()]
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
