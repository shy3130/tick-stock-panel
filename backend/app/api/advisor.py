"""Read-only deterministic recommendation API."""
from __future__ import annotations

import polars as pl
from fastapi import APIRouter, Query, Request

from app.data_providers.trust import load_latest_audits
from app.services import strategy_cache
from app.services.advisor import build_advisor_recommendations

router = APIRouter(prefix="/api/advisor", tags=["advisor"])


def _load_adjustment_event_symbols(data_dir, as_of: str | None) -> set[str]:
    if not as_of:
        return set()
    try:
        frame = pl.read_parquet(
            data_dir / "adj_factor" / "all.parquet",
            columns=["symbol", "trade_date"],
        )
        return set(
            frame.filter(pl.col("trade_date").cast(pl.Utf8) == as_of)
            .get_column("symbol")
            .drop_nulls()
            .cast(pl.Utf8)
            .to_list()
        )
    except Exception:
        return set()


@router.get("/recommendations")
def recommendations(
    request: Request,
    limit: int = Query(30, ge=1, le=100),
) -> dict:
    data_dir = request.app.state.repo.store.data_dir
    cache = strategy_cache.read_cache(data_dir)
    as_of = str(cache.get("as_of")) if isinstance(cache, dict) and cache.get("as_of") else None
    return build_advisor_recommendations(
        load_latest_audits(data_dir),
        cache,
        limit=limit,
        adjustment_event_symbols=_load_adjustment_event_symbols(data_dir, as_of),
    )
