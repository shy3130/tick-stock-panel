"""单笔交易生命周期状态机 + 事件/审计流测试。"""
from __future__ import annotations

import pytest

from app.services.trading import store
from app.services.trading.lifecycle import apply_event, new_trade
from app.services.trading.models import (
    KIND_ADD,
    KIND_ADJUST,
    KIND_CLOSE,
    KIND_FILL,
    KIND_PREPARE,
    KIND_SL,
    KIND_TP,
    LifecycleError,
    STATUS_CLOSED,
    STATUS_HOLDING,
    STATUS_PLANNED,
)

TS = "2026-08-04 14:30"


def _open() -> dict:
    return new_trade(
        "600519.SH_20260804_1",
        "600519.SH",
        {
            "name": "贵州茅台",
            "strategy": "趋势策略",
            "thesis": {"text": "突破年线放量", "invalidation": "跌回年线下方且三日不能收回"},
            "stopLoss": 1600.0,
        },
        TS,
    )


def _holding() -> dict:
    trade = _open()
    trade = apply_event(trade, KIND_PREPARE, {"plannedQty": 100, "plannedPrice": 1680}, TS)
    return apply_event(trade, KIND_FILL, {"qty": 100, "price": 1680.0}, TS)


# ── 建档 ─────────────────────────────────────────────────
def test_open_requires_invalidation():
    with pytest.raises(LifecycleError, match="失效信号"):
        new_trade("t1", "600519.SH", {"name": "x", "thesis": {"text": "论点"}}, TS)


def test_open_requires_thesis_text():
    with pytest.raises(LifecycleError, match="买入论点"):
        new_trade("t1", "600519.SH", {"name": "x", "thesis": {"invalidation": "信号"}}, TS)


def test_open_creates_planned_trade():
    trade = _open()
    assert trade["status"] == STATUS_PLANNED
    assert trade["position"]["qty"] == 0.0
    assert trade["stopLoss"] == 1600.0


# ── fill 约束 ────────────────────────────────────────────
def test_fill_computes_invested_server_side():
    trade = _holding()
    assert trade["status"] == STATUS_HOLDING
    assert trade["position"] == {"qty": 100, "costPrice": 1680.0, "invested": 168000.0}


def test_fill_twice_rejected():
    trade = _holding()
    with pytest.raises(LifecycleError, match="只能发生一次"):
        apply_event(trade, KIND_FILL, {"qty": 100, "price": 1680}, TS)


def test_fill_rejects_non_positive():
    trade = apply_event(_open(), KIND_PREPARE, {"plannedQty": 100}, TS)
    with pytest.raises(LifecycleError, match="正数"):
        apply_event(trade, KIND_FILL, {"qty": -1, "price": 1680}, TS)


# ── 加仓 / 卖出 ──────────────────────────────────────────
def test_add_real_recomputes_avg_cost():
    trade = apply_event(_holding(), KIND_ADD, {"qty": 100, "price": 1700.0}, TS)
    pos = trade["position"]
    assert pos["qty"] == 200
    assert pos["invested"] == 338000.0
    assert pos["costPrice"] == pytest.approx(1690.0)


def test_add_plan_only_does_not_change_position():
    before = _holding()["position"].copy()
    trade = apply_event(_holding(), KIND_ADD, {"planOnly": True, "qty": 100, "price": 1700}, TS)
    assert trade["position"] == before


def test_tp_partial_sell_keeps_cost_and_realizes_pnl():
    trade = apply_event(_holding(), KIND_TP, {"qty": 40, "price": 1750.0}, TS)
    pos = trade["position"]
    assert pos["qty"] == 60
    assert pos["costPrice"] == 1680.0
    assert pos["invested"] == pytest.approx(60 * 1680.0)
    assert trade["realizedPnl"] == pytest.approx((1750.0 - 1680.0) * 40)


def test_sell_full_position_must_use_close():
    with pytest.raises(LifecycleError, match="close"):
        apply_event(_holding(), KIND_SL, {"qty": 100, "price": 1600.0}, TS)


def test_oversell_rejected():
    with pytest.raises(LifecycleError, match="超过当前持仓"):
        apply_event(_holding(), KIND_TP, {"qty": 101, "price": 1750.0}, TS)


def test_sell_before_fill_rejected():
    with pytest.raises(LifecycleError, match="持仓中"):
        apply_event(_open(), KIND_TP, {"qty": 10, "price": 1700.0}, TS)


# ── adjust / close ───────────────────────────────────────
def test_adjust_records_old_stop_loss():
    trade = _holding()
    payload = {"newStopLoss": 1650.0}
    trade = apply_event(trade, KIND_ADJUST, payload, TS)
    assert payload["oldStopLoss"] == 1600.0  # 服务端补录旧值,防篡改
    assert trade["stopLoss"] == 1650.0


def test_adjust_requires_some_change():
    with pytest.raises(LifecycleError, match="newStopLoss 或 newExitRule"):
        apply_event(_holding(), KIND_ADJUST, {}, TS)


def test_close_sells_all_and_finalizes():
    trade = apply_event(_holding(), KIND_TP, {"qty": 40, "price": 1750.0}, TS)
    payload = {"price": 1720.0}
    trade = apply_event(trade, KIND_CLOSE, payload, TS)
    assert payload["qty"] == 60  # 剩余股数由服务端补录
    assert trade["status"] == STATUS_CLOSED
    assert trade["position"]["qty"] == 0.0
    expected = (1750.0 - 1680.0) * 40 + (1720.0 - 1680.0) * 60
    assert trade["realizedPnl"] == pytest.approx(expected)
    assert trade["closedAt"]


def test_write_after_close_rejected():
    trade = apply_event(_holding(), KIND_CLOSE, {"price": 1700.0}, TS)
    with pytest.raises(LifecycleError, match="已平仓"):
        apply_event(trade, KIND_ADJUST, {"newStopLoss": 1600.0}, TS)


# ── 存储: 事件流 / 审计流 append-only ────────────────────
def test_store_roundtrip(tmp_path):
    trade = _open()
    event = {"schemaVersion": 1, "tradeId": trade["tradeId"], "kind": "open", "ts": TS, "payload": {}}
    store.persist_trade_with_event(tmp_path, trade, event)
    loaded = store.read_trade(tmp_path, trade["tradeId"])
    assert loaded["tradeId"] == trade["tradeId"]
    events = store.read_events(tmp_path, trade["tradeId"])
    assert len(events) == 1 and events[0]["kind"] == "open"


def test_trade_id_path_traversal_neutralized(tmp_path):
    trade = _open()
    trade["tradeId"] = "../../evil"
    store.write_trade(tmp_path, trade)
    assert store.read_trade(tmp_path, "../../evil") is not None
    assert not (tmp_path / "evil.json").exists()


def test_audit_append_and_filter(tmp_path):
    store.append_audit(tmp_path, {"ts": TS, "tradeId": "t1", "passed": False, "mode": "buy_new"})
    store.append_audit(tmp_path, {"ts": TS, "tradeId": "t1", "passed": True, "mode": "fill"})
    store.append_audit(tmp_path, {"ts": TS, "tradeId": "t2", "passed": True, "mode": "close"})
    assert len(store.read_audit(tmp_path, trade_id="t1")) == 2
    assert len(store.read_audit(tmp_path, passed=False)) == 1
    assert len(store.read_audit(tmp_path)) == 3


def test_next_trade_seq(tmp_path):
    assert store.next_trade_seq(tmp_path, "600519.SH", "20260804") == 1
    trade = _open()
    store.write_trade(tmp_path, trade)
    assert store.next_trade_seq(tmp_path, "600519.SH", "20260804") == 2
