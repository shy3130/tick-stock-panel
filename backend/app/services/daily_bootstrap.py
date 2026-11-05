"""单标的按需补齐，复用日K路由、指标及仓库写入契约。"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

import polars as pl

from app.indicators.pipeline import compute_enriched
from app.market_time import CN_TZ
from app.services import kline_sync
from app.services.data_integrity import invalid_daily_rows
from app.services.heavy_job_limiter import HeavyJobLimitTimeoutError, shared_heavy_job_limiter
from app.services.shared_work import SharedWork, WorkBusyError

logger = logging.getLogger(__name__)
_work = SharedWork("daily.prepare", max_pending=32, max_entries=128,
                   max_bytes=32 * 1024 * 1024, size_of=lambda frame: frame.estimated_size())


def ensure_history(repo, capset, symbol: str, asset_type: str, start: date, end: date, *, on_updated=None) -> pl.DataFrame:
    # 默认详情与自选共用至少一年窗口，指标有足够预热且不会各自重复补同一标的。
    fetch_start = min(start, end - timedelta(days=365))
    source = kline_sync.preferences.data_source_stamp()
    key = (str(repo.store.data_dir.resolve()), capset, asset_type, symbol, fetch_start, end,
           kline_sync.preferences.get_daily_data_provider(), kline_sync.preferences.get_adj_factor_provider(),
           source, repo.get_enriched_generation_snapshot(asset_type).through(end))

    def run():
        with shared_heavy_job_limiter.slot("normal", timeout=30):
            frame = _ensure_history(repo, capset, symbol, asset_type, fetch_start, end, source)
            if not frame.is_empty() and on_updated:
                on_updated("daily")
            return frame

    frame = _work.run(key, run, ttl_s=30)
    if not frame.is_empty():
        repo.remember_prepared_daily(asset_type, frame)
    return frame.filter(pl.col("date").is_between(start, end)) if not frame.is_empty() else frame


def _ensure_history(repo, capset, symbol: str, asset_type: str, start: date, end: date, source: tuple) -> pl.DataFrame:
    """先读本地；缺数才取源，完整取数且校验成功后写入原有资产目录。"""
    local = repo.get_daily_asset(asset_type, symbol, start, end)
    if not local.is_empty():
        repo.remember_prepared_daily(asset_type, local)
        return local

    frames = list(kline_sync.iter_daily_frames(
        [symbol], capset, asset_type=asset_type,
        start_time=datetime.combine(start, time.min, CN_TZ),
        end_time=datetime.combine(end, time.max, CN_TZ),
    ))
    if not frames:
        return pl.DataFrame()
    raw = kline_sync._normalize_daily(pl.concat(frames, how="diagonal_relaxed"))
    if not invalid_daily_rows(raw).is_empty():
        raise ValueError("日K数据校验失败，原数据保持不变")
    raw = raw.filter((pl.col("symbol") == symbol) & pl.col("date").is_between(start, end)).sort("date")
    if raw.is_empty():
        return raw
    # 网络请求期间实时行情或其他同步可能已补上该标的，优先复用最新本地结果。
    current = repo.get_daily_asset(asset_type, symbol, start, end)
    if not current.is_empty():
        repo.remember_prepared_daily(asset_type, current)
        return current
    factors = pl.DataFrame()
    if asset_type != "index":
        factors = kline_sync.fetch_adj_factor_single(symbol, asset_type=asset_type, capset=capset, strict=True)
    instruments = repo.get_instruments() if asset_type == "stock" else None
    enriched = compute_enriched(raw, factors=factors, instruments=instruments)
    if enriched.is_empty():
        return enriched
    if source != kline_sync.preferences.data_source_stamp():
        raise WorkBusyError("数据源配置已变化，请重新补齐")
    repo.append_daily_asset(asset_type, raw)
    repo.append_enriched_asset(asset_type, enriched)
    daily_table = {"stock": "kline_daily", "etf": "kline_etf_daily", "index": "kline_index_daily"}[asset_type]
    enriched_table = {"stock": "kline_enriched", "etf": "kline_etf_enriched", "index": "kline_index_enriched"}[asset_type]
    enriched_folder = "kline_daily_enriched" if asset_type == "stock" else enriched_table
    root = repo.store.data_dir.as_posix()
    repo.refresh_view(daily_table, f"{root}/{daily_table}/**/*.parquet")
    repo.refresh_view(enriched_table, f"{root}/{enriched_folder}/**/*.parquet")
    repo.remember_prepared_daily(asset_type, enriched)
    return enriched


def prepare(repo, capset, symbols: list[str], end: date, *, on_updated=None) -> list[dict]:
    results = []
    for symbol in symbols:
        try:
            asset = repo.resolve_asset_type(symbol)
            frame = ensure_history(repo, capset, symbol, asset, end - timedelta(days=365), end, on_updated=on_updated)
            status = "ready" if not frame.is_empty() else "no_data"
            message = "" if status == "ready" else "数据源未返回该标的日K，可稍后重试或检查数据源支持范围"
            results.append({"symbol": symbol, "status": status, "message": message})
        except Exception as exc:  # noqa: BLE001 -- 单只失败不阻断其他自选
            from app.services.shared_work import WorkBusyError

            if isinstance(exc, (WorkBusyError, HeavyJobLimitTimeoutError)):
                status, message = "busy", "后台补齐繁忙，请稍后重试"
            elif isinstance(exc, kline_sync.DailySourceUnavailableError):
                status, message = "unsupported", "当前数据源不支持该标的日K，请配置可用数据源"
            else:
                status, message = "failed", "日K补齐失败，请检查数据源或稍后重试"
                logger.warning("自选日K补齐失败 (%s)", symbol, exc_info=True)
            results.append({"symbol": symbol, "status": status, "message": message})
    return results
