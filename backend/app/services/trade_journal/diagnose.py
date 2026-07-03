"""Trade Journal 纯统计诊断。"""
from __future__ import annotations

from collections import defaultdict

from app.services.trade_journal.models import Fill, Roundtrip


def diagnose(
    trips: list[Roundtrip],
    fills: list[Fill],
    price_lookup: dict[tuple[str, str], dict] | None = None,
) -> dict:
    price_lookup = price_lookup or {}
    wins = [t for t in trips if t.total_pnl > 0]
    losses = [t for t in trips if t.total_pnl < 0]
    avg_win_hold = _avg([t.holding_days for t in wins])
    avg_loss_hold = _avg([t.holding_days for t in losses])
    hold_ratio = avg_loss_hold / avg_win_hold if avg_win_hold else 0.0

    months = {t.close_date[:7] for t in trips}
    month_count = max(len(months), 1)
    total_pnl_abs = abs(sum(t.total_pnl for t in trips))
    fees = sum(t.fees for t in trips)

    buy_fills = [f for f in fills if f.side == "buy"]
    covered_buys = [f for f in buy_fills if (f.symbol, f.date) in price_lookup]
    chasing = [
        f
        for f in covered_buys
        if float(price_lookup[(f.symbol, f.date)].get("pos_20d", 0.0)) > 0.9
    ]

    add_count, loss_add_count = _anchoring_counts(buy_fills)
    return {
        "disposition": {
            "avg_win_holding_days": avg_win_hold,
            "avg_loss_holding_days": avg_loss_hold,
            "loss_to_win_holding_ratio": hold_ratio,
            "flag": hold_ratio > 1.5,
        },
        "overtrading": {
            "monthly_roundtrips": len(trips) / month_count,
            "fee_to_abs_pnl": fees / total_pnl_abs if total_pnl_abs else 0.0,
            "flag": (len(trips) / month_count) > 20 or (fees / total_pnl_abs if total_pnl_abs else 0.0) > 0.2,
        },
        "chasing": {
            "covered_buys": len(covered_buys),
            "chasing_buys": len(chasing),
            "ratio": len(chasing) / len(covered_buys) if covered_buys else 0.0,
            "uncovered_buys": len(buy_fills) - len(covered_buys),
            "flag": (len(chasing) / len(covered_buys)) > 0.4 if covered_buys else False,
        },
        "anchoring": {
            "add_buys": add_count,
            "loss_add_buys": loss_add_count,
            "ratio": loss_add_count / add_count if add_count else 0.0,
            "flag": (loss_add_count / add_count) > 0.5 if add_count else False,
        },
    }


def _avg(values: list[float | int]) -> float:
    return sum(values) / len(values) if values else 0.0


def _anchoring_counts(fills: list[Fill]) -> tuple[int, int]:
    by_symbol: dict[str, list[Fill]] = defaultdict(list)
    for fill in sorted(fills, key=lambda f: (f.date, f.time)):
        by_symbol[fill.symbol].append(fill)

    adds = 0
    loss_adds = 0
    for sfills in by_symbol.values():
        qty = 0.0
        cost = 0.0
        for f in sfills:
            if qty > 0:
                adds += 1
                if f.price and f.price < cost / qty:
                    loss_adds += 1
            qty += f.qty
            cost += -f.amount
    return adds, loss_adds
