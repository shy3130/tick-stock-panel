"""组合快照纯函数测试 (不依赖真实 provider)。"""
from __future__ import annotations

import json
from datetime import date, timedelta

import polars as pl

from app.services.trading.models import STATUS_BUILDING, STATUS_CLOSED, STATUS_HOLDING, STATUS_PLANNED
from app.services.trading.portfolio import compute_risk_snapshot, compute_snapshot


def _trade(symbol="600519.SH", qty=100, cost=1680.0, realized=0.0, stop=1600.0, status=STATUS_HOLDING, name="贵州茅台"):
    return {
        "tradeId": f"{symbol}_1",
        "symbol": symbol,
        "name": name,
        "status": status,
        "position": {"qty": qty, "costPrice": cost, "invested": qty * cost},
        "realizedPnl": realized,
        "stopLoss": stop,
        "thesis": {"text": "论点", "invalidation": "跌破1600"},
    }


def _accounts(capital=500000.0, ratio=0.25):
    return {"accounts": [{
        "id": "default", "currency": "CNY",
        "capital": capital, "horizonFundMonths": 12,
        "maxSingleRatio": ratio, "changes": [], "settlements": [],
    }]}


# ── NAV 口径 ─────────────────────────────────────────────
def test_nav_without_prices_only_capital_and_realized():
    trades = [_trade(realized=12000.0), _trade(symbol="000001.SZ", status=STATUS_CLOSED, realized=-5000.0)]
    snap = compute_snapshot(trades, _accounts(), prices={})
    # capital 500000 + realized(12000 - 5000) + unrealized(无价→0)
    assert snap["nav"] == 500000.0 + 7000.0
    assert snap["realizedPnl"] == 7000.0
    assert snap["stale"] is True


def test_nav_with_prices():
    trades = [_trade(qty=100, cost=1680.0)]
    snap = compute_snapshot(trades, _accounts(), prices={"600519.SH": 1750.0})
    # capital 500000 + realized 0 + unrealized (1750-1680)*100 = 7000
    assert snap["nav"] == 507000.0
    assert snap["unrealizedPnl"] == 7000.0
    assert snap["positionsValue"] == 175000.0
    assert snap["stale"] is False


def test_available_subtracts_positions_and_pending():
    trades = [_trade(qty=100, cost=1680.0)]
    snap = compute_snapshot(trades, _accounts(), prices={"600519.SH": 1750.0}, pending_plans_amount=30000.0)
    # nav 507000 - positions 175000 - pending 30000 = 302000
    assert snap["available"] == 302000.0


def test_non_holding_trades_excluded_from_positions():
    trades = [_trade(status=STATUS_PLANNED), _trade(status=STATUS_CLOSED)]
    snap = compute_snapshot(trades, _accounts(), prices={})
    assert snap["positions"] == []
    assert snap["priceSource"] == "无持仓"



def test_building_trade_is_position_and_only_remaining_plan_is_pending():
    trade = _trade(qty=40, cost=100.0, status=STATUS_BUILDING)
    trade["plan"] = {"total": 10000.0}
    trade["build"] = {"filledAmount": 4000.0, "filledQty": 40, "fillCount": 1}
    snap = compute_snapshot([trade], _accounts(capital=50000), prices={"600519.SH": 110.0})
    assert len(snap["positions"]) == 1
    assert snap["positions"][0]["status"] == STATUS_BUILDING
    assert snap["pendingPlansAmount"] == 6000.0
    assert snap["available"] == 50000 + 400 - 4400 - 6000


def test_planned_trade_reserves_full_plan_amount():
    trade = _trade(qty=0, cost=0, status=STATUS_PLANNED)
    trade["plan"] = {"qty": 100, "price": 20, "total": 2000.0}
    snap = compute_snapshot([trade], _accounts(capital=10000), prices={})
    assert snap["pendingPlansAmount"] == 2000.0
    assert snap["available"] == 8000.0


def test_settled_realized_pnl_is_not_counted_twice():
    trade = _trade(status=STATUS_CLOSED, realized=1200.0)
    accounts = _accounts(capital=501200.0)
    accounts["accounts"][0]["settlements"] = [{
        "id": f"settle:{trade['tradeId']}",
        "tradeId": trade["tradeId"],
        "realizedPnl": 1200.0,
    }]
    snap = compute_snapshot([trade], accounts, prices={})
    assert snap["capital"] == 501200.0
    assert snap["realizedPnl"] == 0.0
    assert snap["settledRealizedPnl"] == 1200.0
    assert snap["nav"] == 501200.0

# ── 敞口 / 止损距离 ──────────────────────────────────────
def test_exposure_ratio():
    trades = [_trade(qty=100, cost=1680.0)]
    snap = compute_snapshot(trades, _accounts(), prices={"600519.SH": 1750.0})
    pos = snap["positions"][0]
    # marketValue 175000 / nav 507000
    assert pos["exposure"] == round(175000.0 / 507000.0, 10) or abs(pos["exposure"] - 175000.0 / 507000.0) < 1e-9


def test_stop_loss_distance():
    trades = [_trade(qty=100, cost=1680.0, stop=1600.0)]
    snap = compute_snapshot(trades, _accounts(), prices={"600519.SH": 1750.0})
    pos = snap["positions"][0]
    # (1750 - 1600) / 1750
    assert abs(pos["stopLossDistance"] - (1750.0 - 1600.0) / 1750.0) < 1e-9


# ── health 升级规则 ──────────────────────────────────────
def test_health_normal():
    trades = [_trade(qty=100, cost=1680.0, stop=1600.0)]
    snap = compute_snapshot(trades, _accounts(ratio=0.5), prices={"600519.SH": 1750.0})
    assert snap["health"] == "normal"


def test_health_attention_when_exposure_exceeds_ratio():
    # capital 100000; position value 30000 → exposure 0.3 ∈ (0.25, 0.375)
    trades = [_trade(qty=100, cost=300.0, stop=250.0)]
    snap = compute_snapshot(trades, _accounts(capital=100000.0, ratio=0.25), prices={"600519.SH": 300.0})
    assert snap["health"] == "attention"


def test_health_critical_when_exposure_exceeds_1_5x_ratio():
    # NAV ≈ 100000; position value = 50000 → exposure 0.5 > 1.5×0.25=0.375
    trades = [_trade(qty=100, cost=500.0, stop=400.0)]
    snap = compute_snapshot(trades, _accounts(capital=100000.0, ratio=0.25), prices={"600519.SH": 500.0})
    # 0.5 > 0.375 → critical
    assert snap["health"] == "critical"


def test_health_critical_takes_precedence_over_attention():
    # 两个持仓:一个 attention(超 ratio),一个 critical(超 1.5×ratio)
    t1 = _trade(symbol="A.SH", qty=100, cost=500.0, stop=400.0)
    t2 = _trade(symbol="B.SH", qty=200, cost=500.0, stop=400.0)
    # capital 100000 → nav ~ 200000; B exposure = 100000/200000=0.5>0.375 → critical
    snap = compute_snapshot([t1, t2], _accounts(capital=100000.0, ratio=0.25),
                            prices={"A.SH": 500.0, "B.SH": 500.0})
    assert snap["health"] == "critical"


def test_health_attention_when_price_below_stop():
    trades = [_trade(qty=100, cost=1680.0, stop=1700.0)]
    snap = compute_snapshot(trades, _accounts(ratio=0.9), prices={"600519.SH": 1600.0})
    assert snap["health"] == "attention"


def test_health_attention_when_stale():
    trades = [_trade(qty=100, cost=1680.0, stop=1600.0)]
    snap = compute_snapshot(trades, _accounts(ratio=0.9), prices={})
    assert snap["health"] == "attention"
    assert snap["stale"] is True


def test_price_source_partial():
    trades = [
        _trade(symbol="A.SH", qty=100, cost=100.0, stop=90.0),
        _trade(symbol="B.SH", qty=100, cost=100.0, stop=90.0),
    ]
    snap = compute_snapshot(trades, _accounts(capital=500000.0, ratio=0.9), prices={"A.SH": 110.0})
    assert snap["priceSource"] == "realtime(部分缺失)"
    assert snap["stale"] is True


# ── stale 降级 ───────────────────────────────────────────
def test_stale_when_any_price_missing():
    trades = [
        _trade(symbol="A.SH", qty=100, cost=100.0, stop=90.0),
        _trade(symbol="B.SH", qty=100, cost=100.0, stop=90.0),
    ]
    snap = compute_snapshot(trades, _accounts(), prices={"A.SH": 110.0})
    assert snap["stale"] is True
    assert snap["positions"][0]["stale"] is False
    assert snap["positions"][1]["stale"] is True


def test_case_insensitive_symbol_price_lookup():
    trades = [_trade(symbol="600519.SH")]
    snap = compute_snapshot(trades, _accounts(), prices={"600519.sh": 1750.0})
    assert snap["positions"][0]["price"] == 1750.0
    assert snap["stale"] is False


# ── 多账户 capital 聚合 ──────────────────────────────────
def test_multiple_accounts_capital_summed():
    accounts = {"accounts": [
        {"id": "a", "currency": "CNY", "capital": 200000, "horizonFundMonths": 12, "maxSingleRatio": 0.25, "changes": []},
        {"id": "b", "currency": "CNY", "capital": 300000, "horizonFundMonths": 6, "maxSingleRatio": 0.3, "changes": []},
    ]}
    snap = compute_snapshot([], accounts, prices={})
    assert snap["capital"] == 500000.0
    # maxSingleRatio 取首个账户
    assert snap["maxSingleRatio"] == 0.25


class _RiskRepo:
    def __init__(self, closes: dict[str, list[float]]):
        self.closes = closes

    def get_daily_asset(self, asset_type, symbol, start, end, columns):
        values = self.closes.get(symbol)
        if values is None:
            return pl.DataFrame()
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(len(values))]
        return pl.DataFrame({"symbol": [symbol] * len(values), "date": dates, "close": values})


def test_risk_snapshot_is_deterministic_and_json_safe():
    closes = {
        "600519.SH": [100 + i * 0.5 + (i % 3) for i in range(50)],
        "000001.SZ": [80 + i * 0.2 - (i % 4) * 0.3 for i in range(50)],
    }
    trades = [
        _trade(symbol="600519.SH", qty=100, cost=100),
        _trade(symbol="000001.SZ", qty=200, cost=80),
    ]
    result = compute_risk_snapshot(
        _RiskRepo(closes),
        trades,
        lookback_days=40,
        end=date(2026, 3, 1),
        min_observations=20,
    )
    assert result["status"] == "ok"
    assert result["source"] == "canonical_kline_daily"
    assert result["observations"] == 40
    assert result["metrics"]["annualizedVolatility"] is not None
    assert result["metrics"]["maxDrawdown"] <= 0
    assert abs(sum(row["weight"] for row in result["positions"]) - 1.0) < 1e-5
    contributions = [row["riskContribution"] for row in result["positions"]]
    assert all(value is not None for value in contributions)
    assert abs(sum(contributions) - 1.0) < 1e-5
    json.dumps(result, allow_nan=False)


def test_risk_snapshot_flat_series_uses_null_not_nan():
    trades = [_trade(symbol="600519.SH", qty=100, cost=100)]
    result = compute_risk_snapshot(
        _RiskRepo({"600519.SH": [100.0] * 30}),
        trades,
        lookback_days=25,
        end=date(2026, 3, 1),
        min_observations=20,
    )
    assert result["status"] == "ok"
    assert result["metrics"]["annualizedVolatility"] == 0.0
    assert result["positions"][0]["riskContribution"] is None
    assert result["correlation"]["matrix"] == [[None]]
    json.dumps(result, allow_nan=False)


def test_risk_snapshot_without_positions_is_explicit():
    result = compute_risk_snapshot(
        _RiskRepo({}),
        [_trade(status=STATUS_PLANNED, qty=0)],
    )
    assert result["status"] == "no_positions"
    assert result["degraded"] is False
