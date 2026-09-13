"""指数 API (核心四只固定清单, 浏览/搜索全量指数已下线; 仅保留详情读数与同步)。"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request

from app.indicators.pipeline import compute_enriched
from app.services import index_sync, kline_sync
from app.tickflow.capabilities import Cap

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/index", tags=["index"])

# 当日指数分钟结果进程内缓存: 吸收板块切换卡片/指数页 30s 轮询与重挂载的重复请求
_INDEX_MINUTE_CACHE_TTL = 10.0
_INDEX_MINUTE_CACHE_MAX = 32
_index_minute_cache: dict[tuple[str, str], tuple[float, pl.DataFrame]] = {}


def _index_info(repo, symbol: str) -> dict:
    df = repo.get_index_instruments()
    if df.is_empty() or "symbol" not in df.columns:
        return {}
    hit = df.filter(pl.col("symbol") == symbol).head(1)
    if hit.is_empty():
        return {}
    return hit.to_dicts()[0]



@router.get("/daily")
def get_index_daily(
    request: Request,
    symbol: str = Query(..., description="指数代码, 如 000001.SH"),
    days: int = Query(120, ge=10, le=2000),
    start_date: Optional[str] = Query(None, description="起始日期 YYYY-MM-DD, 优先于 days"),
    end_date: Optional[str] = Query(None, description="截止日期 YYYY-MM-DD, 默认今天"),
):
    """读取指数日 K。指数数据使用独立 kline_index_* parquet。"""
    repo = request.app.state.repo
    end = date.fromisoformat(end_date) if end_date else date.today()
    start = date.fromisoformat(start_date) if start_date else end - timedelta(days=days)
    info = _index_info(repo, symbol)

    df = repo.get_index_daily(symbol, start, end)
    if not df.is_empty():
        return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": df.to_dicts(), "source": "index_enriched"}

    capset = request.app.state.capabilities
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": [], "source": "none"}

    try:
        raw = kline_sync.sync_daily_batch([symbol], count=days + 150)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"TickFlow fetch failed: {e}") from e
    if raw.is_empty():
        return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": [], "source": "none"}

    enriched = compute_enriched(raw, factors=None, instruments=None)
    rows = enriched.filter((pl.col("date") >= start) & (pl.col("date") <= end)).to_dicts()
    return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": rows, "source": "live"}


@router.get("/minute")
def get_index_minute(
    request: Request,
    symbol: str = Query(..., description="指数代码, 如 000001.SH"),
    trade_date: date | None = Query(None, alias="date", description="交易日期, 默认今天"),
):
    """实时读取指数分钟 K。不写入股票分钟 parquet。

    仅当日有效: 本地无指数分钟存储, 实时数据源也不提供历史分时, 非当日请求
    直接返回空 (source=not_today), 不做徒劳的数据源网络等待。
    当日结果带 10s 进程内缓存, 重复轮询只打一次数据源。
    """
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    info = _index_info(repo, symbol)
    day = trade_date or date.today()
    if day != date.today():
        return {
            "symbol": symbol,
            "name": info.get("name"),
            "index_info": info,
            "date": str(day),
            "rows": [],
            "source": "not_today",
        }
    cache_key = (symbol, day.isoformat())
    now = time.monotonic()
    hit = _index_minute_cache.get(cache_key)
    if hit is not None and now - hit[0] < _INDEX_MINUTE_CACHE_TTL:
        df = hit[1]
    else:
        df = kline_sync.fetch_minute_single(symbol, day, asset_type="index", capset=capset)
        _index_minute_cache[cache_key] = (now, df)
        while len(_index_minute_cache) > _INDEX_MINUTE_CACHE_MAX:
            oldest = min(_index_minute_cache, key=lambda k: _index_minute_cache[k][0])
            if oldest == cache_key:
                break
            del _index_minute_cache[oldest]
    return {
        "symbol": symbol,
        "name": info.get("name"),
        "index_info": info,
        "date": str(day),
        "rows": df.to_dicts(),
        "source": "live" if not df.is_empty() else "none",
    }


@router.post("/sync_instruments")
def sync_index_instruments(request: Request):
    """同步 CN_Index 指数标的列表。"""
    repo = request.app.state.repo
    count = index_sync.sync_index_instruments(repo)
    return {"status": "ok", "count": count}


@router.post("/sync_daily")
def sync_index_daily(
    request: Request,
    days: int = Query(365, ge=30, le=5000),
):
    """同步指数日K到独立 parquet。"""
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        raise HTTPException(status_code=403, detail="需要 Pro+ 权限 (batch K-line)")
    end = datetime.now()
    start = end - timedelta(days=days)
    count = index_sync.sync_index_instruments(repo)
    rows = index_sync.sync_and_persist_index_daily(repo, capset, start_date=start, end_date=end)
    return {"status": "ok", "index_count": count, "rows_written": rows}
