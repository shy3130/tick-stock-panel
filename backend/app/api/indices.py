"""指数 API。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request

from app.indicators.pipeline import compute_enriched
from app.services import index_sync, kline_sync
from app.capabilities import Cap

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/index", tags=["index"])


def _index_info(repo, symbol: str) -> dict:
    from app.data_providers.fquant.symbols import canonical_index_symbol
    symbol = canonical_index_symbol(symbol)
    df = repo.get_index_instruments()
    if df.is_empty() or "symbol" not in df.columns:
        return {}
    hit = df.filter(pl.col("symbol") == symbol).head(1)
    if hit.is_empty():
        return {}
    return hit.to_dicts()[0]


@router.get("/list")
def list_indices(request: Request):
    """返回已缓存的 CN_Index 指数列表。"""
    repo = request.app.state.repo
    df = repo.get_index_instruments()
    if df.is_empty():
        return {"results": [], "count": 0}
    cols = [c for c in ["symbol", "name", "code", "asset_type"] if c in df.columns]
    rows = df.select(cols).sort("symbol").to_dicts()
    return {"results": rows, "count": len(rows)}


@router.get("/search")
def search_indices(
    request: Request,
    q: str = Query("", min_length=0, max_length=50, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=100),
):
    """模糊搜索指数。"""
    repo = request.app.state.repo
    df = repo.get_index_instruments()
    if df.is_empty():
        return {"results": []}
    if not q.strip():
        rows = df.head(limit).to_dicts()
        return {"results": rows}

    from app.services.symbol_search import search_symbols

    matched = search_symbols(
        repo,
        q,
        limit,
        asset_type="index",
        use_suggest=False,
        max_limit=100,
    )
    original_by_symbol = {
        str(row.get("symbol")): row
        for row in df.to_dicts()
        if row.get("symbol") is not None
    }
    rows = [
        {**original_by_symbol.get(str(row.get("symbol")), {}), **row}
        for row in matched
    ]
    return {"results": rows}


@router.get("/daily")
def get_index_daily(
    request: Request,
    symbol: str = Query(..., description="指数代码, 如 000001.INDEX"),
    days: int = Query(120, ge=10, le=2000),
    start_date: Optional[str] = Query(None, description="起始日期 YYYY-MM-DD, 优先于 days"),
    end_date: Optional[str] = Query(None, description="截止日期 YYYY-MM-DD, 默认今天"),
):
    """读取指数日 K。指数数据使用独立 kline_index_* parquet。"""
    from app.data_providers.fquant.symbols import canonical_index_symbol
    symbol = canonical_index_symbol(symbol)
    repo = request.app.state.repo
    end = date.fromisoformat(end_date) if end_date else date.today()
    start = date.fromisoformat(start_date) if start_date else end - timedelta(days=days)
    info = _index_info(repo, symbol)

    from app.services.data_mode import is_local_daily_mode
    if is_local_daily_mode():
        # 本地磁盘模式：provider 直查上游快照（最新），避免本地 parquet 滞后导致
        # 返回陈旧数据（本地 index parquet 由盘后管道写，盘中/盘后早期会落后一天）。
        from app.data_providers.registry import get_active_provider_name, get_provider

        provider = get_provider(get_active_provider_name("daily"))
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.min.time())
        raw = provider.get_daily([symbol], start_dt, end_dt, "index")
        if not raw.is_empty():
            enriched = compute_enriched(raw, factors=None, instruments=None)
            rows = enriched.filter((pl.col("date") >= start) & (pl.col("date") <= end)).to_dicts()
            return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": rows, "source": "local_disk"}

    df = repo.get_index_daily(symbol, start, end)
    if not df.is_empty():
        return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": df.to_dicts(), "source": "index_enriched"}

    capset = request.app.state.capabilities
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": [], "source": "none"}

    try:
        raw = kline_sync.sync_daily_batch([symbol], count=days + 150, asset_type="index")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"数据源拉取失败: {e}") from e
    if raw.is_empty():
        return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": [], "source": "none"}

    enriched = compute_enriched(raw, factors=None, instruments=None)
    rows = enriched.filter((pl.col("date") >= start) & (pl.col("date") <= end)).to_dicts()
    return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": rows, "source": "live"}


@router.get("/minute")
def get_index_minute(
    request: Request,
    symbol: str = Query(..., description="指数代码, 如 000001.INDEX"),
    trade_date: date | None = Query(None, alias="date", description="交易日期, 默认今天"),
):
    """实时读取指数分钟 K。不写入股票分钟 parquet。"""
    from app.data_providers.fquant.symbols import canonical_index_symbol
    symbol = canonical_index_symbol(symbol)
    repo = request.app.state.repo
    info = _index_info(repo, symbol)
    day = trade_date or date.today()
    df = kline_sync.fetch_minute_single(symbol, day, asset_type="index")
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
        raise HTTPException(status_code=403, detail="当前数据源不支持批量日K")
    end = datetime.now()
    start = end - timedelta(days=days)
    count = index_sync.sync_index_instruments(repo)
    rows = index_sync.sync_and_persist_index_daily(repo, capset, start_date=start, end_date=end)
    return {"status": "ok", "index_count": count, "rows_written": rows}
