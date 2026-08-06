"""组合快照 — 纯函数,派生、可重建 (不是事实源)。

输入:
- trades: ``store.list_trades`` 的结果 (单笔事实列表)
- accounts: ``accounts.read_accounts`` 的结果 (结构红线输入)
- prices: {symbol: 最新价 | None};None 表示取不到 (stale)

输出 NAV / 持仓 / 健康度快照。不访问 provider,纯计算 —— 可脱离真实行情单测。

口径:
- NAV = capital + Σ(所有 trade 的 realizedPnl) + Σ(持仓浮动盈亏)
- 剩余可开 = NAV - 持仓市值合计 - pending_plans_amount
- exposure = marketValue / NAV
- stopLossDistance = (price - stopLoss) / price  (price 与 stopLoss 均可得时)
- health (取最严重):
    敞口 > 1.5×maxSingleRatio            → critical
    敞口 > maxSingleRatio / 价格跌破止损 / 价格 stale → attention
    否则                                  → normal
- stale = 任一持仓取不到价格;priceSource 说明行情覆盖情况
"""
from __future__ import annotations

from typing import Any

from app.services.trading.models import STATUS_HOLDING

_HEALTH_RANK = {"normal": 0, "attention": 1, "critical": 2}


def compute_snapshot(
    trades: list[dict[str, Any]],
    accounts: dict[str, Any],
    prices: dict[str, float | None],
    pending_plans_amount: float = 0.0,
) -> dict[str, Any]:
    accs = accounts.get("accounts") or []
    capital = float(sum(_num(a.get("capital")) for a in accs))
    # A 股先单币种: 取首个账户的 maxSingleRatio 作为敞口红线的结构输入
    max_single = _num(accs[0].get("maxSingleRatio")) if accs else 0.25

    realized = float(sum(_num(t.get("realizedPnl")) for t in trades))

    positions: list[dict[str, Any]] = []
    positions_value = 0.0
    unrealized_total = 0.0
    any_stale = False
    holding_count = 0

    for t in trades:
        if t.get("status") != STATUS_HOLDING:
            continue
        holding_count += 1
        pos = t.get("position") or {}
        qty = _num(pos.get("qty"))
        cost = _num(pos.get("costPrice"))
        symbol = str(t.get("symbol") or "")
        price = _lookup_price(prices, symbol)
        stop = t.get("stopLoss")
        stale = price is None
        if stale:
            any_stale = True

        market_value: float | None = None
        unrealized: float | None = None
        sl_distance: float | None = None
        if not stale:
            market_value = qty * price
            unrealized = (price - cost) * qty
            positions_value += market_value
            unrealized_total += unrealized
            if stop is not None and price:
                sl_distance = (price - stop) / price

        positions.append({
            "tradeId": t.get("tradeId"),
            "symbol": symbol,
            "name": t.get("name"),
            "qty": qty,
            "costPrice": cost,
            "price": price,
            "marketValue": market_value,
            "unrealizedPnl": unrealized,
            "stopLoss": stop,
            "stopLossDistance": sl_distance,
            "thesis": t.get("thesis"),
            "stale": stale,
        })

    nav = capital + realized + unrealized_total
    available = nav - positions_value - pending_plans_amount

    health = "normal"
    for p in positions:
        mv = p["marketValue"]
        p["exposure"] = (mv / nav) if (nav and mv is not None) else None
        health = _worse(health, _position_health(p, max_single))

    return {
        "nav": nav,
        "capital": capital,
        "realizedPnl": realized,
        "unrealizedPnl": unrealized_total,
        "positionsValue": positions_value,
        "available": available,
        "pendingPlansAmount": float(pending_plans_amount),
        "positions": positions,
        "health": health,
        "stale": bool(any_stale),
        "priceSource": _price_source(holding_count, positions),
        "maxSingleRatio": max_single,
    }


def _position_health(p: dict[str, Any], max_single: float) -> str:
    if p["stale"]:
        return "attention"
    exposure = p["exposure"]
    if exposure is not None:
        if exposure > 1.5 * max_single:
            return "critical"
        if exposure > max_single:
            return "attention"
    stop = p["stopLoss"]
    price = p["price"]
    if stop is not None and price is not None and price < stop:
        return "attention"
    return "normal"


def _worse(a: str, b: str) -> str:
    return a if _HEALTH_RANK[a] >= _HEALTH_RANK[b] else b


def _price_source(holding_count: int, positions: list[dict[str, Any]]) -> str:
    if holding_count == 0:
        return "无持仓"
    missing = sum(1 for p in positions if p["price"] is None)
    if missing == 0:
        return "realtime"
    if missing == len(positions):
        return "实时行情不可用"
    return "realtime(部分缺失)"


def _num(v: Any) -> float:
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return 0.0


def _lookup_price(prices: dict[str, float | None], symbol: str) -> float | None:
    """大小写无关查价: 标的 / 价格键 两端都可能带不同大小写。"""
    if not symbol:
        return None
    if symbol in prices:
        return prices[symbol]
    up = symbol.upper()
    low = symbol.lower()
    if up in prices:
        return prices[up]
    if low in prices:
        return prices[low]
    # 键的原始大小写不统一时逐个比对
    for k, v in prices.items():
        if k and k.upper() == up:
            return v
    return None
