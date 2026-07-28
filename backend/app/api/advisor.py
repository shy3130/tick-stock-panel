"""Read-only deterministic recommendation API."""
from __future__ import annotations

import polars as pl
from fastapi import APIRouter, Query, Request

from app.data_providers.trust import load_latest_audits
from app.services import strategy_cache
from app.services.advisor import build_advisor_recommendations, build_beginner_daily_brief

router = APIRouter(prefix="/api/advisor", tags=["advisor"])

_ADJ_FACTOR_RUNTIME_PROBLEM = {
    "code": "ADJ_FACTOR_RUNTIME_UNAVAILABLE",
    "reason": "除权因子文件缺失、无法读取或结构不完整, 无法核对策略日期的除权除息事件",
    "next_action": (
        "请重新同步除权因子, 并确认 all.parquet 包含 symbol、trade_date 列后"
        "再重新生成研究清单。"
    ),
}


def _load_adjustment_event_symbols(
    data_dir,
    as_of: str | None,
) -> tuple[set[str], dict[str, str] | None]:
    if not as_of:
        return set(), None
    try:
        frame = pl.read_parquet(
            data_dir / "adj_factor" / "all.parquet",
            columns=["symbol", "trade_date"],
        )
        if frame.schema["symbol"] != pl.String or frame.schema["trade_date"] != pl.Date:
            raise ValueError("unexpected adjustment-factor schema")
        if frame.filter(
            pl.col("symbol").is_null()
            | (pl.col("symbol").str.strip_chars() == "")
            | pl.col("trade_date").is_null()
        ).height:
            raise ValueError("invalid adjustment-factor values")
    except Exception:
        return set(), dict(_ADJ_FACTOR_RUNTIME_PROBLEM)
    symbols = set(
        frame.filter(pl.col("trade_date").cast(pl.Utf8) == as_of)
        .get_column("symbol")
        .drop_nulls()
        .cast(pl.Utf8)
        .to_list()
    )
    return symbols, None


def _persisted_recommendations(request: Request, *, limit: int) -> dict:
    data_dir = request.app.state.repo.store.data_dir
    cache = strategy_cache.read_cache(data_dir)
    as_of = str(cache.get("as_of")) if isinstance(cache, dict) and cache.get("as_of") else None
    adjustment_event_symbols, adjustment_factor_problem = _load_adjustment_event_symbols(
        data_dir,
        as_of,
    )
    return build_advisor_recommendations(
        load_latest_audits(data_dir),
        cache,
        limit=limit,
        adjustment_event_symbols=adjustment_event_symbols,
        adjustment_factor_problem=adjustment_factor_problem,
    )


@router.get("/recommendations")
def recommendations(
    request: Request,
    limit: int = Query(30, ge=1, le=100),
) -> dict:
    return _persisted_recommendations(request, limit=limit)


@router.get("/daily-brief")
def daily_brief(request: Request) -> dict:
    return build_beginner_daily_brief(_persisted_recommendations(request, limit=3))
