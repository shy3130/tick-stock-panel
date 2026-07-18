"""指数 API。"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request

from app.indicators.pipeline import compute_enriched
from app.services import index_sync, kline_sync
from app.tickflow.capabilities import Cap

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/index", tags=["index"])


def _rows_for_json(df: pl.DataFrame) -> list[dict]:
    rows = df.to_dicts()
    for row in rows:
        for key, value in row.items():
            if isinstance(value, float) and not math.isfinite(value):
                row[key] = None
    return rows


def _index_info(repo, symbol: str) -> dict:
    df = repo.get_index_instruments()
    if df.is_empty() or "symbol" not in df.columns:
        return {}
    hit = df.filter(pl.col("symbol") == symbol).head(1)
    if hit.is_empty():
        return {}
    return hit.to_dicts()[0]


def _filter_market(df: pl.DataFrame, market: str | None) -> pl.DataFrame:
    normalized = str(market or "").strip().lower()
    if normalized not in {"cn", "hk", "us"}:
        return df
    if "market" in df.columns:
        return df.filter(pl.col("market").cast(pl.Utf8).str.to_lowercase() == normalized)
    if normalized == "hk":
        return df.filter(pl.col("symbol").str.to_uppercase().str.ends_with(".HK"))
    if normalized == "us":
        return df.filter(pl.col("symbol").str.to_uppercase().str.ends_with(".US"))
    upper_symbol = pl.col("symbol").str.to_uppercase()
    return df.filter(
        ~upper_symbol.str.ends_with(".HK") & ~upper_symbol.str.ends_with(".US")
    )


@router.get("/list")
def list_indices(request: Request, market: str | None = Query(None, pattern="^(cn|hk|us)$")):
    """按市场返回已缓存的指数列表。"""
    repo = request.app.state.repo
    df = repo.get_index_instruments()
    if df.is_empty():
        return {"results": [], "count": 0}
    df = _filter_market(df, market)
    cols = [c for c in ["symbol", "name", "code", "market", "asset_type"] if c in df.columns]
    rows = df.select(cols).sort("symbol").to_dicts()
    return {"results": rows, "count": len(rows)}


@router.get("/search")
def search_indices(
    request: Request,
    q: str = Query("", min_length=0, max_length=50, description="搜索关键词"),
    limit: int = Query(20, ge=1, le=100),
    market: str | None = Query(None, pattern="^(cn|hk|us)$"),
):
    """模糊搜索指数。"""
    repo = request.app.state.repo
    df = repo.get_index_instruments()
    if df.is_empty():
        return {"results": []}
    df = _filter_market(df, market)
    if not q.strip():
        rows = df.head(limit).to_dicts()
        return {"results": rows}

    keyword = q.strip().upper()
    masks = []
    if "code" in df.columns:
        masks.append(pl.col("code").cast(pl.Utf8).str.contains(keyword, literal=True))
    masks.append(pl.col("symbol").cast(pl.Utf8).str.to_uppercase().str.contains(keyword, literal=True))
    if "name" in df.columns:
        masks.append(pl.col("name").cast(pl.Utf8).str.contains(q.strip(), literal=True))

    mask = masks[0]
    for m in masks[1:]:
        mask = mask | m
    rows = df.filter(mask).head(limit).to_dicts()
    return {"results": rows}


@router.get("/daily")
def get_index_daily(
    request: Request,
    symbol: str = Query(..., description="指数代码, 如 000001.SH"),
    days: int = Query(120, ge=10, le=2000),
    start_date: str | None = Query(None, description="起始日期 YYYY-MM-DD, 优先于 days"),
    end_date: str | None = Query(None, description="截止日期 YYYY-MM-DD, 默认今天"),
):
    """读取指数日 K。指数数据使用独立 kline_index_* parquet。"""
    repo = request.app.state.repo
    end = date.fromisoformat(end_date) if end_date else date.today()
    start = date.fromisoformat(start_date) if start_date else end - timedelta(days=days)
    info = _index_info(repo, symbol)

    df = repo.get_index_daily(symbol, start, end)
    if not df.is_empty():
        return {
            "symbol": symbol,
            "name": info.get("name"),
            "index_info": info,
            "rows": _rows_for_json(df),
            "source": "index_enriched",
        }

    capset = request.app.state.capabilities
    try:
        from app.services import preferences
        provider_name = preferences.get_daily_data_provider()
        if provider_name != "tickflow":
            from app.data_providers import custom as custom_sources
            provider = custom_sources.get_provider(provider_name)
            raw = provider.get_daily(
                [symbol],
                start_time=datetime.combine(start, datetime.min.time()),
                end_time=datetime.combine(end, datetime.max.time()),
                asset_type="index",
            )
        elif capset.has(Cap.KLINE_DAILY_BATCH):
            raw = kline_sync.sync_daily_batch([symbol], count=days + 150)
        else:
            raw = pl.DataFrame()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"指数数据获取失败: {e}") from e
    if raw.is_empty():
        return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": [], "source": "none"}

    enriched = compute_enriched(raw, factors=None, instruments=None)
    repo.append_index_daily(raw)
    repo.append_index_enriched(enriched)
    repo.refresh_index_views()
    rows = _rows_for_json(
        enriched.filter((pl.col("date") >= start) & (pl.col("date") <= end))
    )
    return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": rows, "source": "live"}


@router.get("/minute")
def get_index_minute(
    request: Request,
    symbol: str = Query(..., description="指数代码, 如 000001.SH"),
    trade_date: date | None = Query(None, alias="date", description="交易日期, 默认今天"),
):
    """实时读取指数分钟 K。不写入股票分钟 parquet。"""
    repo = request.app.state.repo
    info = _index_info(repo, symbol)
    day = trade_date or date.today()
    df = kline_sync.fetch_minute_single(symbol, day)
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
    from app.services import preferences
    provider_name = preferences.get_daily_data_provider()
    has_custom_daily = False
    if provider_name != "tickflow":
        from app.data_providers import custom as custom_sources
        has_custom_daily = custom_sources.provider_has_dataset(provider_name, "daily")
    if not capset.has(Cap.KLINE_DAILY_BATCH) and not has_custom_daily:
        raise HTTPException(status_code=403, detail="需要 Pro+ 权限 (batch K-line)")
    end = datetime.now()
    start = end - timedelta(days=days)
    count = index_sync.sync_index_instruments(repo)
    rows = index_sync.sync_and_persist_index_daily(repo, capset, start_date=start, end_date=end)
    return {"status": "ok", "index_count": count, "rows_written": rows}
