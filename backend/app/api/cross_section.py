"""Cross-section analysis API — correlation, relative strength, peer comparison, reverse screen.

All four endpoints are pure GET, read-only, local-only.  They never write data,
issue external requests, or produce trading signals.  Reverse-screen re-uses the
existing ``execute_query`` screener path.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.services.cross_section import (
    compute_correlation_matrix,
    compute_peer_comparison,
    compute_relative_strength,
    compute_reverse_screen,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cross-section", tags=["cross-section"])


@router.get("/correlation")
def correlation(
    request: Request,
    symbol: str = Query(..., description="标的代码, 如 000001.SZ"),
    window: int = Query(120, ge=1, le=365, description="收益率窗口 (有效值: 60/120/180)"),
    min_samples: int = Query(20, ge=1, le=365, description="最小共同交易日"),
    max_peers: int = Query(6, ge=1, le=20, description="peer 候选上限"),
) -> dict[str, Any]:
    """Pairwise Pearson correlation matrix of daily returns within the peer universe."""
    repo = request.app.state.repo
    try:
        return compute_correlation_matrix(
            repo, symbol,
            window=window, min_samples=min_samples, max_peers=max_peers,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/relative-strength")
def relative_strength(
    request: Request,
    symbol: str = Query(..., description="标的代码, 如 000001.SZ"),
    days: int = Query(120, ge=1, le=400, description="回溯交易日上限"),
    benchmark: str = Query("000001.INDEX", description="基准指数 (000001.INDEX / 399001.INDEX / 399006.INDEX)"),
) -> dict[str, Any]:
    """Stock NAV vs benchmark NAV with 10/20/60-day window return comparison."""
    repo = request.app.state.repo
    try:
        return compute_relative_strength(repo, symbol, days=days, benchmark=benchmark)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/peer-comparison")
def peer_comparison(
    request: Request,
    symbol: str = Query(..., description="标的代码, 如 000001.SZ"),
    mode: str = Query("industry", description="universe 模式 (industry / amount / board)"),
    limit: int = Query(12, ge=1, le=50, description="展示行数"),
    sort_key: str = Query("amount", description="排序键 (amount / change_pct / turnover_rate / roe / pe / pb / score)"),
) -> dict[str, Any]:
    """Rank a stock within its peer universe by market or fundamental metrics."""
    repo = request.app.state.repo
    try:
        return compute_peer_comparison(repo, symbol, mode=mode, limit=limit, sort_key=sort_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/reverse-screen")
def reverse_screen(
    request: Request,
    symbol: str = Query(..., description="标的代码, 如 000001.SZ"),
) -> dict[str, Any]:
    """以股找股 — build relaxed screener conditions from the stock's latest features.

    Results are research candidates only; no buy/sell signals are produced.
    """
    repo = request.app.state.repo
    return compute_reverse_screen(repo, symbol)
