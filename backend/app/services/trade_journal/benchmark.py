"""基准超额统计。"""
from __future__ import annotations

from app.services.trade_journal.models import Roundtrip

NOISE_NOTE = "基准超额仅用于方向性复盘, 未按账户现金流精确加权。"


def account_excess(trips: list[Roundtrip], index_closes: dict[str, float]) -> dict:
    if not trips:
        return {"pnl": 0.0, "account_return": 0.0, "benchmark_return": None, "excess": None, "window": None}
    invested = sum(t.buy_net for t in trips)
    pnl = sum(t.total_pnl for t in trips)
    start, end = min(t.open_date for t in trips), max(t.close_date for t in trips)
    bench = _ret(index_closes, start, end)
    ret = pnl / invested if invested else 0.0
    return {
        "pnl": pnl,
        "account_return": ret,
        "benchmark_return": bench,
        "excess": ret - bench if bench is not None else None,
        "window": [start, end],
    }


def per_trip_excess(trips: list[Roundtrip], index_closes: dict[str, float]) -> list[dict]:
    rows: list[dict] = []
    for trip in trips:
        if trip.symbol.endswith(".HK"):
            bench = excess = None
        else:
            bench = _ret(index_closes, trip.open_date, trip.close_date)
            excess = trip.pnl_pct - bench if bench is not None else None
        rows.append(
            {
                "symbol": trip.symbol,
                "open_date": trip.open_date,
                "close_date": trip.close_date,
                "pnl_pct": trip.pnl_pct,
                "benchmark_pct": bench,
                "excess": excess,
            }
        )
    return rows


def _ret(closes: dict[str, float], start: str, end: str) -> float | None:
    a, b = closes.get(start), closes.get(end)
    if not a or not b:
        return None
    return b / a - 1.0
