from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Request

from app.backtest.patterns import detect_patterns

router = APIRouter(prefix="/api/patterns", tags=["patterns"])


@router.get("/{symbol}")
def get_patterns(
    symbol: str,
    request: Request,
    lookback: int = 120,
    asset_type: Literal["stock", "etf", "index"] = "stock",
) -> dict:
    repo = request.app.state.repo
    end = date.today()
    start = end - timedelta(days=max(lookback * 2, 180))
    columns = ["date", "open", "high", "low", "close", "volume"]
    if asset_type == "etf" and hasattr(repo, "get_etf_daily"):
        df = repo.get_etf_daily(symbol, start, end, columns)
    elif asset_type == "index" and hasattr(repo, "get_index_daily"):
        df = repo.get_index_daily(symbol, start, end, columns)
    else:
        df = repo.get_daily(symbol, start, end, columns)
    if df.is_empty():
        return {"symbol": symbol, "as_of": None, "patterns": []}
    df = df.sort("date").tail(max(lookback, 5))
    as_of = str(df["date"][-1])[:10]
    return {"symbol": symbol, "as_of": as_of, "patterns": detect_patterns(df, lookback)}
