"""组合快照纯函数测试 (不依赖真实 provider)。"""
from __future__ import annotations

from app.services.trading.accounts import read_accounts
from app.services.trading.models import STATUS_HOLDING, STATUS_CLOSED, STATUS_PLANNED
from app.services.trading.portfolio import compute_snapshot


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
        "maxSingleRatio": ratio, "changes": [],
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
