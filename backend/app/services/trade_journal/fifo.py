"""Position-cycle FIFO 配对。"""
from __future__ import annotations

import bisect
from collections import defaultdict
from datetime import date

from app.services.trade_journal.models import CashEvent, Fill, Roundtrip

_EPS = 1e-6


def _holding_days(open_date: str, close_date: str, trading_days: list[str] | None) -> int:
    if trading_days:
        lo = bisect.bisect_left(trading_days, open_date)
        hi = bisect.bisect_right(trading_days, close_date)
        if hi > lo:
            return hi - lo
    try:
        return (date.fromisoformat(close_date) - date.fromisoformat(open_date)).days + 1
    except ValueError:
        return 1


def pair_roundtrips(
    fills: list[Fill],
    events: list[CashEvent],
    trading_days: list[str] | None,
) -> tuple[list[Roundtrip], list[dict], list[str]]:
    warnings: list[str] = []
    by_symbol: dict[tuple[str, str], list[Fill]] = defaultdict(list)
    for fill in sorted(fills, key=lambda f: (f.date, f.time)):
        by_symbol[(fill.account_id, fill.symbol)].append(fill)

    div_by_symbol: dict[tuple[str, str], list[CashEvent]] = defaultdict(list)
    for ev in events:
        if ev.kind in ("dividend", "dividend_tax") and ev.symbol:
            div_by_symbol[(ev.account_id, ev.symbol)].append(ev)

    trips: list[Roundtrip] = []
    open_positions: list[dict] = []
    for (account_id, symbol), sfills in by_symbol.items():
        pos = 0.0
        cycle: list[Fill] = []
        for idx, fill in enumerate(sfills):
            f = fill
            if f.side == "sell" and f.qty > pos + _EPS:
                warnings.append(f"{symbol} {f.date} 卖出 {f.qty:g} 超过持仓 {pos:g}, 超出部分跳过")
                if pos <= _EPS:
                    continue
                scale = pos / f.qty
                f = Fill(f.date, f.time, f.symbol, f.name, f.side, pos, f.price, f.amount * scale, f.fee * scale, f.account_id)
            cycle.append(f)
            pos += f.qty if f.side == "buy" else -f.qty
            if pos <= _EPS and cycle:
                nxt = sfills[idx + 1] if idx + 1 < len(sfills) else None
                if nxt is not None and nxt.side == "buy" and nxt.date == f.date:
                    continue
                trips.append(_close_cycle(account_id, symbol, cycle, trading_days))
                cycle, pos = [], 0.0
        if cycle and pos > _EPS:
            buy_net = sum(-f.amount for f in cycle if f.side == "buy")
            open_positions.append(
                {
                    "symbol": symbol,
                    "account_id": account_id,
                    "name": cycle[-1].name,
                    "qty": pos,
                    "open_date": cycle[0].date,
                    "cost_net": round(buy_net, 2),
                }
            )
    trips.sort(key=lambda t: t.close_date)
    _apply_dividends(trips, div_by_symbol)
    return trips, open_positions, warnings


def _close_cycle(
    account_id: str,
    symbol: str,
    cycle: list[Fill],
    trading_days: list[str] | None,
) -> Roundtrip:
    open_date, close_date = cycle[0].date, cycle[-1].date
    return Roundtrip(
        symbol=symbol,
        name=cycle[-1].name,
        open_date=open_date,
        close_date=close_date,
        qty=sum(f.qty for f in cycle if f.side == "buy"),
        buy_net=sum(-f.amount for f in cycle if f.side == "buy"),
        sell_net=sum(f.amount for f in cycle if f.side == "sell"),
        fees=sum(f.fee for f in cycle),
        dividend=0.0,
        holding_days=_holding_days(open_date, close_date, trading_days),
        account_id=account_id,
    )


def _apply_dividends(trips: list[Roundtrip], div_by_symbol: dict[tuple[str, str], list[CashEvent]]) -> None:
    by_symbol: dict[tuple[str, str], list[Roundtrip]] = defaultdict(list)
    for trip in trips:
        by_symbol[(trip.account_id, trip.symbol)].append(trip)
    for sym_trips in by_symbol.values():
        sym_trips.sort(key=lambda t: (t.open_date, t.close_date))

    for key, events in div_by_symbol.items():
        sym_trips = by_symbol.get(key, [])
        for ev in sorted(events, key=lambda e: e.date):
            target = _dividend_target(sym_trips, ev)
            if target is not None:
                target.dividend += ev.amount


def _dividend_target(trips: list[Roundtrip], ev: CashEvent) -> Roundtrip | None:
    if ev.kind == "dividend_tax":
        if any(trip.open_date == ev.date for trip in trips):
            return None
        previous = [trip for trip in trips if trip.close_date < ev.date]
        if previous:
            for trip in trips:
                if trip.open_date < ev.date <= trip.close_date:
                    return trip
            return previous[-1]
    for trip in trips:
        if trip.open_date <= ev.date <= trip.close_date:
            return trip
    if ev.kind != "dividend_tax":
        return None
    previous = [trip for trip in trips if trip.close_date < ev.date]
    return previous[-1] if previous else None
