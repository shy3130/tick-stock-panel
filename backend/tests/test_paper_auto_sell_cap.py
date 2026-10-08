"""自动跟单卖出按可卖持仓封顶。

卖出规则的仓位只能按金额/权益换算股数, 与实际持仓对不上: 换算多了整单被
create_order 以「可卖数量不足」拒绝, 换算少了只卖一部分。自动卖出应以可卖
数量(扣除已挂 pending 卖单)为上限, 金额设足够大即等价于清仓。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.strategy import paper, paper_auto
from app.tickflow.repository import DataStore, KlineRepository

SYM = "002893.SZ"
DAY = date(2026, 10, 8)


def _write_prev_close(tmp_path) -> None:
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(pl.DataFrame({
        "symbol": [SYM], "date": [DAY - timedelta(days=1)],
        "open": [10.0], "high": [10.0], "low": [10.0], "close": [10.0],
        "volume": [10000.0], "amount": [100000.0],
    }))


@pytest.fixture
def holding_500(tmp_path, monkeypatch):
    """昨日买入 500 股, 今日可卖。"""
    monkeypatch.setattr(paper, "cn_today", lambda: DAY - timedelta(days=1))
    _write_prev_close(tmp_path)
    paper.create_account(tmp_path, 100_000.0)
    _, err = paper.create_order(tmp_path, SYM, "buy", qty=500, ref_price=10.0)
    assert err is None
    assert len(paper.evaluate_intraday(tmp_path, {SYM: 10.0})) == 1
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    paper._materialize(tmp_path)
    return tmp_path


def _sell_rule(**overrides) -> dict:
    rule = {
        "name": "12元清仓", "match_kind": "rule", "match_id": "price_ge_12",
        "side": "sell", "size_mode": "fixed_amount", "size_value": 10_000_000.0,
        "order_type": "market", "cooldown_days": 0, "enabled": True,
    }
    rule.update(overrides)
    return rule


def _event(price: float = 12.0) -> dict:
    return {"source": "price", "rule_id": "price_ge_12", "symbol": SYM, "price": price}


def test_auto_sell_amount_above_holding_sells_whole_position(holding_500):
    paper_auto.create_auto_rule(holding_500, _sell_rule())

    orders = paper_auto.on_rule_events(holding_500, [_event()])

    assert len(orders) == 1
    assert orders[0]["side"] == "sell"
    assert orders[0]["qty"] == 500


def test_auto_sell_cap_excludes_pending_sell_orders(holding_500):
    _, err = paper.create_order(holding_500, SYM, "sell", qty=200, ref_price=12.0)
    assert err is None
    paper_auto.create_auto_rule(holding_500, _sell_rule())

    orders = paper_auto.on_rule_events(holding_500, [_event()])

    assert [o["qty"] for o in orders] == [300]


def test_auto_sell_amount_below_holding_keeps_partial_size(holding_500):
    paper_auto.create_auto_rule(holding_500, _sell_rule(size_value=2400.0))

    orders = paper_auto.on_rule_events(holding_500, [_event()])

    assert [o["qty"] for o in orders] == [200]


def test_auto_sell_without_position_creates_no_order(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    _write_prev_close(tmp_path)
    paper.create_account(tmp_path, 100_000.0)
    paper_auto.create_auto_rule(tmp_path, _sell_rule())

    assert paper_auto.on_rule_events(tmp_path, [_event()]) == []
