"""组合快照 — 纯函数,派生、可重建 (不是事实源)。

输入:
- trades: ``store.list_trades`` 的结果 (单笔事实列表)
- accounts: ``accounts.read_accounts`` 的结果 (结构红线输入)
- prices: {symbol: 最新价 | None};None 表示取不到 (stale)

输出 NAV / 持仓 / 健康度快照。不访问 provider,纯计算 —— 可脱离真实行情单测。

口径:
- NAV = capital + 未结转已实现盈亏 + 持仓浮动盈亏；
- 已进入 settlements 的 realizedPnl 已并入 capital，不再重复相加；
- 剩余可开 = NAV - 持仓市值合计 - 待建计划；
- 计划中计全额计划，建仓中只计 plan.total - build.filledAmount；
- 建仓中与持仓中都是真实市场敞口。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np

from app.backtest.portfolio import load_price_panel, returns_from_prices
from app.json_safe import finite_float_or_none
from app.services.trading.accounts import settled_trade_ids
from app.services.trading.models import STATUS_BUILDING, STATUS_HOLDING, STATUS_PLANNED

_HEALTH_RANK = {"normal": 0, "attention": 1, "critical": 2}


def compute_snapshot(
    trades: list[dict[str, Any]],
    accounts: dict[str, Any],
    prices: dict[str, float | None],
    pending_plans_amount: float | None = None,
) -> dict[str, Any]:
    accs = accounts.get("accounts") or []
    capital = float(sum(_num(a.get("capital")) for a in accs))
    # A 股先单币种: 取首个账户的 maxSingleRatio 作为敞口红线的结构输入
    max_single = _num(accs[0].get("maxSingleRatio")) if accs else 0.25

    settled_ids = settled_trade_ids(accounts)
    realized = float(
        sum(
            _num(t.get("realizedPnl"))
            for t in trades
            if str(t.get("tradeId") or "") not in settled_ids
        )
    )
    settled_realized = float(
        sum(
            _num(t.get("realizedPnl"))
            for t in trades
            if str(t.get("tradeId") or "") in settled_ids
        )
    )
    if pending_plans_amount is None:
        pending_plans_amount = _pending_plans(trades)

    positions: list[dict[str, Any]] = []
    positions_value = 0.0
    unrealized_total = 0.0
    any_stale = False
    holding_count = 0

    for t in trades:
        if t.get("status") not in (STATUS_BUILDING, STATUS_HOLDING):
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
            "status": t.get("status"),
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
        "settledRealizedPnl": settled_realized,
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


def _pending_plans(trades: list[dict[str, Any]]) -> float:
    total = 0.0
    for trade in trades:
        status = trade.get("status")
        plan = trade.get("plan") or {}
        plan_total = _num(plan.get("total"))
        if plan_total <= 0:
            plan_total = _num(plan.get("qty")) * _num(plan.get("price"))
        if plan_total <= 0:
            continue
        if status == STATUS_PLANNED:
            total += plan_total
        elif status == STATUS_BUILDING:
            build = trade.get("build") or {}
            filled = _num(build.get("filledAmount"))
            if filled <= 0:
                filled = _num((trade.get("position") or {}).get("invested"))
            total += max(0.0, plan_total - filled)
    return round(total, 2)


def compute_risk_snapshot(
    repo: Any,
    trades: list[dict[str, Any]],
    *,
    lookback_days: int = 120,
    end: date | None = None,
    min_observations: int = 20,
) -> dict[str, Any]:
    """用 canonical 日 K 对当前真实持仓做静态权重风险回放。"""
    quantities: dict[str, float] = {}
    for trade in trades:
        if trade.get("status") not in (STATUS_BUILDING, STATUS_HOLDING):
            continue
        symbol = str(trade.get("symbol") or "").strip()
        qty = _num((trade.get("position") or {}).get("qty"))
        if symbol and qty > 0:
            quantities[symbol] = quantities.get(symbol, 0.0) + qty

    symbols = sorted(quantities)
    base = {
        "lookbackDays": lookback_days,
        "source": "canonical_kline_daily",
        "methodology": "当前持仓按窗口末日收盘市值定权；日收益内连接；风险贡献基于样本协方差；最大回撤为静态权重历史回放。",
    }
    if not symbols:
        return {
            **base,
            "status": "no_positions",
            "degraded": False,
            "dataAsOf": None,
            "observations": 0,
            "metrics": _empty_risk_metrics(),
            "positions": [],
            "correlation": {"symbols": [], "matrix": []},
            "meta": {"kept": [], "dropped": [], "warnings": []},
        }

    end_date = end or date.today()
    start_date = end_date - timedelta(days=max(lookback_days * 2, lookback_days + 30))
    panel, kept = load_price_panel(repo, symbols, start_date, end_date)
    dropped = [symbol for symbol in symbols if symbol not in kept]
    warnings = [f"{symbol}: canonical 日 K 不足，未纳入风险估计" for symbol in dropped]
    if panel.height > lookback_days + 1:
        panel = panel.tail(lookback_days + 1)
    if not kept or panel.height < 2:
        return _insufficient_risk(base, kept, dropped, warnings, panel)

    prices = panel.select(kept).to_numpy().astype(float)
    finite_rows = np.isfinite(prices).all(axis=1) & (prices > 0).all(axis=1)
    prices = prices[finite_rows]
    if prices.shape[0] < 2:
        warnings.append("共同窗口内没有足够的有限正收盘价")
        return _insufficient_risk(base, kept, dropped, warnings, panel)
    returns = returns_from_prices(prices)
    clean = returns[np.isfinite(returns).all(axis=1)]
    observations = int(clean.shape[0])
    last_prices = prices[-1]
    market_values = np.asarray([quantities[symbol] * last_prices[i] for i, symbol in enumerate(kept)], dtype=float)
    total_value = float(market_values.sum())
    if not np.isfinite(total_value) or total_value <= 0:
        warnings.append("窗口末日持仓市值不可用")
        return _insufficient_risk(base, kept, dropped, warnings, panel)
    weights = market_values / total_value

    data_as_of = _last_panel_date(panel)
    if observations < min_observations:
        warnings.append(f"共同收益样本仅 {observations} 条，低于最小值 {min_observations}")
        positions = [
            {
                "symbol": symbol,
                "weight": round(float(weights[i]), 6),
                "annualizedVolatility": None,
                "riskContribution": None,
            }
            for i, symbol in enumerate(kept)
        ]
        return {
            **base,
            "status": "insufficient_data",
            "degraded": True,
            "dataAsOf": data_as_of,
            "observations": observations,
            "metrics": _empty_risk_metrics(),
            "positions": positions,
            "correlation": {"symbols": kept, "matrix": []},
            "meta": {"kept": kept, "dropped": dropped, "warnings": warnings},
        }

    cov = np.atleast_2d(np.cov(clean, rowvar=False))
    portfolio_returns = clean @ weights
    portfolio_variance = max(float(weights @ cov @ weights), 0.0)
    annualized_vol = float(np.sqrt(portfolio_variance * 252))
    asset_vol = np.sqrt(np.maximum(np.diag(cov), 0.0) * 252)
    risk_contribution: np.ndarray | None = None
    if portfolio_variance > 0:
        risk_contribution = weights * (cov @ weights) / portfolio_variance

    standard_deviation = np.std(clean, axis=0, ddof=1)
    denominator = np.outer(standard_deviation, standard_deviation)
    corr = np.full(cov.shape, np.nan, dtype=float)
    np.divide(cov, denominator, out=corr, where=denominator > 0)
    corr_matrix = [
        [_finite_round(float(value), 6) for value in row]
        for row in corr
    ]
    off_diagonal = corr[~np.eye(len(kept), dtype=bool)] if len(kept) > 1 else np.array([])
    finite_pairs = off_diagonal[np.isfinite(off_diagonal)]
    max_pair = float(np.max(finite_pairs)) if finite_pairs.size else None
    curve = np.cumprod(1.0 + portfolio_returns)
    peaks = np.maximum.accumulate(curve)
    drawdowns = curve / peaks - 1.0
    max_drawdown = float(np.min(drawdowns)) if drawdowns.size else 0.0
    hhi = float(np.sum(weights ** 2))

    positions = []
    for i, symbol in enumerate(kept):
        positions.append({
            "symbol": symbol,
            "weight": round(float(weights[i]), 6),
            "annualizedVolatility": _finite_round(float(asset_vol[i]), 6),
            "riskContribution": (
                _finite_round(float(risk_contribution[i]), 6)
                if risk_contribution is not None
                else None
            ),
        })
    positions.sort(key=lambda item: item["weight"], reverse=True)
    return {
        **base,
        "status": "ok",
        "degraded": bool(dropped),
        "dataAsOf": data_as_of,
        "observations": observations,
        "metrics": {
            "annualizedVolatility": _finite_round(annualized_vol, 6),
            "maxDrawdown": _finite_round(max_drawdown, 6),
            "maxPairCorrelation": _finite_round(max_pair, 6) if max_pair is not None else None,
            "effectivePositions": _finite_round(1.0 / hhi, 4) if hhi > 0 else None,
            "topWeight": _finite_round(float(np.max(weights)), 6),
        },
        "positions": positions,
        "correlation": {"symbols": kept, "matrix": corr_matrix},
        "meta": {"kept": kept, "dropped": dropped, "warnings": warnings},
    }


def _empty_risk_metrics() -> dict[str, None]:
    return {
        "annualizedVolatility": None,
        "maxDrawdown": None,
        "maxPairCorrelation": None,
        "effectivePositions": None,
        "topWeight": None,
    }


def _insufficient_risk(
    base: dict[str, Any],
    kept: list[str],
    dropped: list[str],
    warnings: list[str],
    panel: Any,
) -> dict[str, Any]:
    return {
        **base,
        "status": "insufficient_data",
        "degraded": True,
        "dataAsOf": _last_panel_date(panel),
        "observations": max(int(panel.height) - 1, 0),
        "metrics": _empty_risk_metrics(),
        "positions": [],
        "correlation": {"symbols": kept, "matrix": []},
        "meta": {"kept": kept, "dropped": dropped, "warnings": warnings},
    }


def _last_panel_date(panel: Any) -> str | None:
    if panel.height == 0 or "date" not in panel.columns:
        return None
    value = panel["date"][-1]
    return value.isoformat() if hasattr(value, "isoformat") else str(value)[:10]


def _finite_round(value: float, digits: int) -> float | None:
    return round(value, digits) if np.isfinite(value) else None


def _num(v: Any) -> float:
    return finite_float_or_none(v) or 0.0


def _lookup_price(prices: dict[str, float | None], symbol: str) -> float | None:
    """大小写无关查价，并把 NaN/Inf 视为缺失。"""
    if not symbol:
        return None
    value: Any = None
    if symbol in prices:
        value = prices[symbol]
    else:
        up = symbol.upper()
        low = symbol.lower()
        if up in prices:
            value = prices[up]
        elif low in prices:
            value = prices[low]
        else:
            for key, candidate in prices.items():
                if key and key.upper() == up:
                    value = candidate
                    break
    return finite_float_or_none(value)
