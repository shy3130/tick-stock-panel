"""门禁引擎结构红线测试 — 逐条红线 + 全过场景 + gate_rules 读写。"""
from __future__ import annotations

import json

import pytest

from app.services.trading import store
from app.services.trading.gates import (
    evaluate_gates,
    read_gate_rules,
    write_gate_rules,
)

TS = "2026-08-04 14:30"


# ── 账户 + 持仓脚手架 ───────────────────────────────────
def _setup_accounts(data_dir, capital=500000.0, ratio=0.25, horizon=12):
    """写一份 accounts.json (绕过 write_accounts 的 changes append-only 校验,直接落盘)。"""
    p = data_dir / "user_data" / "trading" / "accounts.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "schemaVersion": 1,
        "accounts": [{
            "id": "default", "currency": "CNY",
            "capital": capital, "horizonFundMonths": horizon,
            "maxSingleRatio": ratio, "changes": [],
        }],
    }, ensure_ascii=False), encoding="utf-8")


def _make_trade(symbol="600519.SH", qty=0, cost=1680.0, stop=1600.0):
    """构造一个 trade dict (evaluate_gates 需要)。"""
    return {
        "tradeId": f"{symbol}_20260804_1",
        "symbol": symbol,
        "name": "测试标的",
        "status": "持仓中" if qty > 0 else "计划中",
        "position": {"qty": qty, "costPrice": cost, "invested": qty * cost},
        "realizedPnl": 0.0,
        "stopLoss": stop,
        "thesis": {"text": "论点", "invalidation": "跌破1600"},
    }


def _write_event(data_dir, trade_id, kind, ts, payload=None):
    store.append_event(data_dir, {
        "schemaVersion": 1, "tradeId": trade_id, "kind": kind, "ts": ts,
        "payload": payload or {}, "note": "", "gateBypassed": False,
    })


# ── single_position_ratio ───────────────────────────────
def test_single_position_ratio_passes_when_within_limit(tmp_path):
    _setup_accounts(tmp_path, capital=500000.0, ratio=0.25)
    # 买入金额 100000 / NAV 500000 = 0.2 ≤ 0.25
    result = evaluate_gates(tmp_path, "buy_new", trade=None, payload={"qty": 100, "price": 1000})
    gate = _gate(result, "single_position_ratio")
    assert gate["passed"] is True


def test_single_position_ratio_rejected_when_exceeding(tmp_path):
    _setup_accounts(tmp_path, capital=500000.0, ratio=0.25)
    # 买入金额 150000 / NAV 500000 = 0.3 > 0.25
    result = evaluate_gates(tmp_path, "buy_new", trade=None, payload={"qty": 150, "price": 1000})
    gate = _gate(result, "single_position_ratio")
    assert gate["passed"] is False
    assert result["passed"] is False
    assert "single_position_ratio" in result["missing"]


def test_single_position_ratio_uses_amount_field(tmp_path):
    _setup_accounts(tmp_path, capital=100000.0, ratio=0.25)
    # amount=30000 / 100000 = 0.3 > 0.25
    result = evaluate_gates(tmp_path, "add", trade=None, payload={"amount": 30000})
    gate = _gate(result, "single_position_ratio")
    assert gate["passed"] is False


def test_single_position_ratio_skipped_when_no_amount(tmp_path):
    _setup_accounts(tmp_path)
    result = evaluate_gates(tmp_path, "buy_new", trade=None, payload={})
    gate = _gate(result, "single_position_ratio")
    assert gate["passed"] is True  # 无金额跳过


# ── stop_loss_defined ───────────────────────────────────
def test_stop_loss_defined_rejected_without_any_exit(tmp_path):
    _setup_accounts(tmp_path)
    trade = _make_trade(stop=None)
    trade["thesis"] = {"text": "论点"}  # 无 invalidation
    result = evaluate_gates(tmp_path, "buy_new", trade=trade, payload={})
    gate = _gate(result, "stop_loss_defined")
    assert gate["passed"] is False
    assert result["passed"] is False


def test_stop_loss_defined_passes_with_stop_price(tmp_path):
    _setup_accounts(tmp_path)
    result = evaluate_gates(tmp_path, "buy_new", trade=None, payload={"stopLoss": 1600.0})
    gate = _gate(result, "stop_loss_defined")
    assert gate["passed"] is True


def test_stop_loss_defined_passes_with_exit_rule(tmp_path):
    _setup_accounts(tmp_path)
    result = evaluate_gates(tmp_path, "buy_new", trade=None, payload={"exitRule": "跌破年线卖出"})
    gate = _gate(result, "stop_loss_defined")
    assert gate["passed"] is True


def test_stop_loss_defined_passes_with_thesis_invalidation(tmp_path):
    _setup_accounts(tmp_path)
    trade = _make_trade(stop=None)
    # trade 有 thesis.invalidation
    result = evaluate_gates(tmp_path, "buy_new", trade=trade, payload={})
    gate = _gate(result, "stop_loss_defined")
    assert gate["passed"] is True


# ── stop_loss_distance ──────────────────────────────────
def test_stop_loss_distance_positive_passes(tmp_path):
    _setup_accounts(tmp_path)
    # price 1680, stopLoss 1600 → distance (1680-1600)/1680 > 0
    result = evaluate_gates(tmp_path, "buy_new", trade=None, payload={"price": 1680.0, "stopLoss": 1600.0})
    gate = _gate(result, "stop_loss_distance")
    assert gate["passed"] is True


def test_stop_loss_distance_zero_rejected(tmp_path):
    _setup_accounts(tmp_path)
    result = evaluate_gates(tmp_path, "buy_new", trade=None, payload={"price": 1600.0, "stopLoss": 1600.0})
    gate = _gate(result, "stop_loss_distance")
    assert gate["passed"] is False
    assert result["passed"] is False


def test_stop_loss_distance_negative_rejected(tmp_path):
    _setup_accounts(tmp_path)
    # stop 1700 > price 1680 → distance 负
    result = evaluate_gates(tmp_path, "buy_new", trade=None, payload={"price": 1680.0, "stopLoss": 1700.0})
    gate = _gate(result, "stop_loss_distance")
    assert gate["passed"] is False


def test_stop_loss_distance_for_adjust_uses_new_stop(tmp_path):
    _setup_accounts(tmp_path)
    trade = _make_trade(qty=100, cost=1680.0)
    # adjust: newStopLoss 1650 < cost 1680 → 正
    result = evaluate_gates(tmp_path, "adjust", trade=trade, payload={"newStopLoss": 1650.0})
    gate = _gate(result, "stop_loss_distance")
    assert gate["passed"] is True


def test_stop_loss_distance_for_adjust_rejected_when_above_cost(tmp_path):
    _setup_accounts(tmp_path)
    trade = _make_trade(qty=100, cost=1680.0)
    result = evaluate_gates(tmp_path, "adjust", trade=trade, payload={"newStopLoss": 1750.0})
    gate = _gate(result, "stop_loss_distance")
    assert gate["passed"] is False


# ── horizon_match ───────────────────────────────────────
def test_horizon_match_passes_when_within_limit(tmp_path):
    _setup_accounts(tmp_path, horizon=12)
    result = evaluate_gates(tmp_path, "buy_new", trade=None, payload={"thesisHorizonMonths": 6})
    gate = _gate(result, "horizon_match")
    assert gate["passed"] is True


def test_horizon_match_rejected_when_exceeding(tmp_path):
    _setup_accounts(tmp_path, horizon=6)
    result = evaluate_gates(tmp_path, "buy_new", trade=None, payload={"thesisHorizonMonths": 12})
    gate = _gate(result, "horizon_match")
    assert gate["passed"] is False
    assert result["passed"] is False


def test_horizon_match_skipped_when_not_declared(tmp_path):
    _setup_accounts(tmp_path, horizon=6)
    result = evaluate_gates(tmp_path, "buy_new", trade=None, payload={})
    gate = _gate(result, "horizon_match")
    assert gate["passed"] is True
    assert "未声明" in gate["detail"]


# ── fill_reconciliation ─────────────────────────────────
def test_fill_reconciliation_passes_when_within_threshold(tmp_path):
    _setup_accounts(tmp_path)
    trade = _make_trade(qty=0)
    store.write_trade(tmp_path, trade)
    _write_event(tmp_path, trade["tradeId"], "prepare", TS, {"plannedQty": 100, "plannedPrice": 1680.0})
    # fill 100@1680 = 168000, plan 100@1680 = 168000 → 偏差 0
    result = evaluate_gates(tmp_path, "fill", trade=trade, payload={"qty": 100, "price": 1680.0, "complete": True})
    gate = _gate(result, "fill_reconciliation")
    assert gate["passed"] is True


def test_fill_reconciliation_rejected_when_deviation_exceeds_without_reason(tmp_path):
    _setup_accounts(tmp_path)
    trade = _make_trade(qty=0)
    store.write_trade(tmp_path, trade)
    _write_event(tmp_path, trade["tradeId"], "prepare", TS, {"plannedQty": 100, "plannedPrice": 1680.0})
    # plan 168000, fill 200000 → 偏差 ~19% > 10%, 无 reconcileReason
    result = evaluate_gates(tmp_path, "fill", trade=trade, payload={"qty": 100, "price": 2000.0, "complete": True})
    gate = _gate(result, "fill_reconciliation")
    assert gate["passed"] is False
    assert result["passed"] is False


def test_fill_reconciliation_passes_when_deviation_exceeds_with_reason(tmp_path):
    _setup_accounts(tmp_path)
    trade = _make_trade(qty=0)
    store.write_trade(tmp_path, trade)
    _write_event(tmp_path, trade["tradeId"], "prepare", TS, {"plannedQty": 100, "plannedPrice": 1680.0})
    result = evaluate_gates(tmp_path, "fill", trade=trade,
                            payload={"qty": 100, "price": 2000.0, "complete": True, "reconcileReason": "集合竞价跳空"})
    gate = _gate(result, "fill_reconciliation")
    assert gate["passed"] is True


def test_fill_reconciliation_skipped_when_no_plan(tmp_path):
    _setup_accounts(tmp_path)
    trade = _make_trade(qty=0)
    store.write_trade(tmp_path, trade)
    # 无 prepare 事件
    result = evaluate_gates(tmp_path, "fill", trade=trade, payload={"qty": 100, "price": 2000.0})
    gate = _gate(result, "fill_reconciliation")
    assert gate["passed"] is True


# ── 全过场景 ─────────────────────────────────────────────
def test_all_gates_pass_buy_new(tmp_path):
    _setup_accounts(tmp_path, capital=500000.0, ratio=0.5)
    result = evaluate_gates(tmp_path, "buy_new", trade=None, payload={
        "qty": 100, "price": 1680.0, "stopLoss": 1600.0, "thesisHorizonMonths": 6,
    })
    assert result["passed"] is True
    assert result["missing"] == []
    assert len(result["gates"]) == 4


def test_modes_without_gates_return_passed(tmp_path):
    _setup_accounts(tmp_path)
    for mode in ("tp", "sl", "close"):
        result = evaluate_gates(tmp_path, mode, trade=None, payload={})
        assert result["passed"] is True
        assert result["gates"] == []


def test_unknown_mode_returns_passed(tmp_path):
    _setup_accounts(tmp_path)
    result = evaluate_gates(tmp_path, "prepare", trade=None, payload={})
    assert result["passed"] is True
    assert result["gates"] == []


def test_gate_result_has_required_fields(tmp_path):
    _setup_accounts(tmp_path)
    result = evaluate_gates(tmp_path, "buy_new", trade=None, payload={"qty": 1, "price": 1})
    for g in result["gates"]:
        assert "id" in g and "name" in g and "passed" in g and "detail" in g


# ── gate_rules 读写 ──────────────────────────────────────
def test_read_gate_rules_default_when_absent(tmp_path):
    rules = read_gate_rules(tmp_path)
    assert rules["schemaVersion"] == 1
    assert "buy_new" in rules["rules"]
    assert rules["rules"]["buy_new"] == {"all": [], "any": [], "discipline": []}


def test_write_then_read_gate_rules(tmp_path):
    payload = {
        "rules": {
            "buy_new": {
                "all": [{"id": "trend_up", "text": "趋势向上"}],
                "any": [{"id": "vol_ok", "text": "放量"}],
                "discipline": [{"id": "no_chase", "text": "不追涨停"}],
            },
        },
    }
    written = write_gate_rules(tmp_path, payload)
    assert written["rules"]["buy_new"]["all"][0]["id"] == "trend_up"
    # 读写一致
    read_back = read_gate_rules(tmp_path)
    assert read_back["rules"]["buy_new"]["all"] == [{"id": "trend_up", "text": "趋势向上"}]
    # 缺省 mode 补空清单
    assert read_back["rules"]["add"] == {"all": [], "any": [], "discipline": []}


def test_write_gate_rules_normalizes_invalid_entries(tmp_path):
    payload = {"rules": {"buy_new": {"all": [{"id": "", "text": "x"}, {"id": "ok", "text": ""}, "not_dict"]}}}
    written = write_gate_rules(tmp_path, payload)
    # 空 id / 空 text / 非对象 → 全部过滤
    assert written["rules"]["buy_new"]["all"] == []


def test_write_gate_rules_rejects_non_dict(tmp_path):
    with pytest.raises(ValueError, match="gate_rules"):
        write_gate_rules(tmp_path, [])  # type: ignore[arg-type]


def test_write_gate_rules_rejects_bad_rules_field(tmp_path):
    with pytest.raises(ValueError, match="rules"):
        write_gate_rules(tmp_path, {"rules": []})


def test_read_gate_rules_recovers_from_corrupt_file(tmp_path):
    p = tmp_path / "user_data" / "trading" / "gate_rules.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json {{{", encoding="utf-8")
    rules = read_gate_rules(tmp_path)
    assert rules["rules"]["buy_new"]["all"] == []


# ── 工具 ─────────────────────────────────────────────────
def _gate(result: dict, gate_id: str) -> dict:
    for g in result["gates"]:
        if g["id"] == gate_id:
            return g
    pytest.fail(f"gate {gate_id} not found in result")


# ── 服务端集成: fill 偏差 → 422 / confirmed 绕过 ─────────
from fastapi import HTTPException as _HTTPException  # noqa: E402


def _setup_fill_flow(data_dir, trade, plan_price=1680.0):
    """写 trade + open/prepare 事件流, 返回 trade_id。"""
    store.write_trade(data_dir, trade)
    _write_event(data_dir, trade["tradeId"], "open", TS)
    _write_event(data_dir, trade["tradeId"], "prepare", TS,
                 {"plannedQty": 100, "plannedPrice": plan_price})


def test_server_fill_deviation_rejected_without_reason(tmp_path, monkeypatch):
    from app.api import trading

    monkeypatch.setattr(trading.settings, "data_dir", tmp_path)
    _setup_accounts(tmp_path)
    trade = _make_trade(qty=0)
    _setup_fill_flow(tmp_path, trade, plan_price=1680.0)

    # fill 100@2000 = 200000 vs plan 168000 → 偏差 >10%, 无 reconcileReason, 无 confirmed
    with pytest.raises(_HTTPException) as exc:
        trading.append_event(trade["tradeId"], {
            "kind": "fill",
            "payload": {"qty": 100, "price": 2000.0, "complete": True},
        })
    assert exc.value.status_code == 422

    # 审计留痕: passed=false
    from app.services.trading import store as tstore
    audit = tstore.read_audit(tmp_path, trade_id=trade["tradeId"], passed=False)
    assert len(audit) == 1
    assert audit[0]["mode"] == "fill"
    assert audit[0]["passed"] is False


def test_server_fill_deviation_passes_with_reason(tmp_path, monkeypatch):
    from app.api import trading

    monkeypatch.setattr(trading.settings, "data_dir", tmp_path)
    _setup_accounts(tmp_path)
    trade = _make_trade(qty=0)
    _setup_fill_flow(tmp_path, trade, plan_price=1680.0)

    # 偏差 >10% 但有 reconcileReason → 通过
    updated = trading.append_event(trade["tradeId"], {
        "kind": "fill",
        "payload": {"qty": 100, "price": 2000.0, "complete": True, "reconcileReason": "集合竞价跳空高开"},
    })
    assert updated["status"] == "持仓中"
    assert updated["position"]["qty"] == 100

    from app.services.trading import store as tstore
    audit = tstore.read_audit(tmp_path, trade_id=trade["tradeId"], passed=True)
    assert any(a["mode"] == "fill" and a["passed"] is True for a in audit)


def test_server_buy_new_ratio_rejected(tmp_path, monkeypatch):
    from app.api import trading

    monkeypatch.setattr(trading.settings, "data_dir", tmp_path)
    _setup_accounts(tmp_path, capital=100000.0, ratio=0.10)
    # 买入金额 100*200=20000 / NAV 100000 = 0.2 > 0.1 → 拒绝
    with pytest.raises(_HTTPException) as exc:
        trading.open_trade({
            "symbol": "600519.SH",
            "name": "茅台",
            "thesis": {"text": "突破", "invalidation": "跌破1600"},
            "stopLoss": 1600.0,
            "qty": 100,
            "price": 200.0,
        })
    assert exc.value.status_code == 422


def test_server_buy_new_confirmed_bypasses_gate(tmp_path, monkeypatch):
    from app.api import trading

    monkeypatch.setattr(trading.settings, "data_dir", tmp_path)
    _setup_accounts(tmp_path, capital=100000.0, ratio=0.10)
    # 结构红线失败 (比例超限) 但 confirmed=true → 落盘 + gateBypassed
    trade = trading.open_trade({
        "symbol": "600519.SH",
        "name": "茅台",
        "thesis": {"text": "突破", "invalidation": "跌破1600"},
        "stopLoss": 1600.0,
        "qty": 100,
        "price": 200.0,
        "gate": {"confirmed": True},
    })
    assert trade["status"] == "计划中"

    from app.services.trading import store as tstore
    events = tstore.read_events(tmp_path, trade["tradeId"])
    assert len(events) == 1
    assert events[0]["gateBypassed"] is True
    audit = tstore.read_audit(tmp_path, trade_id=trade["tradeId"], passed=False)
    assert len(audit) == 1


def test_server_fill_confirmed_bypasses_deviation(tmp_path, monkeypatch):
    from app.api import trading

    monkeypatch.setattr(trading.settings, "data_dir", tmp_path)
    _setup_accounts(tmp_path)
    trade = _make_trade(qty=0)
    _setup_fill_flow(tmp_path, trade, plan_price=1680.0)
    # 偏差 >10%, 无 reason, 但 confirmed → 落盘 + gateBypassed
    updated = trading.append_event(trade["tradeId"], {
        "kind": "fill",
        "payload": {"qty": 100, "price": 2000.0, "complete": True},
        "gate": {"confirmed": True},
    })
    assert updated["status"] == "持仓中"
    from app.services.trading import store as tstore
    events = tstore.read_events(tmp_path, trade["tradeId"])
    fill_events = [e for e in events if e["kind"] == "fill"]
    assert fill_events[0]["gateBypassed"] is True


def test_server_fill_within_threshold_no_gate_needed(tmp_path, monkeypatch):
    from app.api import trading

    monkeypatch.setattr(trading.settings, "data_dir", tmp_path)
    _setup_accounts(tmp_path)
    trade = _make_trade(qty=0)
    _setup_fill_flow(tmp_path, trade, plan_price=1680.0)
    # fill 100@1690 = 169000 vs 168000 → 偏差 <1% → 通过, gateBypassed=false
    updated = trading.append_event(trade["tradeId"], {
        "kind": "fill",
        "payload": {"qty": 100, "price": 1690.0, "complete": True},
    })
    assert updated["status"] == "持仓中"
    from app.services.trading import store as tstore
    events = tstore.read_events(tmp_path, trade["tradeId"])
    fill_events = [e for e in events if e["kind"] == "fill"]
    assert fill_events[0]["gateBypassed"] is False


def test_server_close_settles_realized_pnl_once(tmp_path, monkeypatch):
    from app.api import trading
    from app.services.trading.accounts import read_accounts

    monkeypatch.setattr(trading.settings, "data_dir", tmp_path)
    _setup_accounts(tmp_path)
    trade = _make_trade(qty=0)
    _setup_fill_flow(tmp_path, trade, plan_price=1680.0)
    holding = trading.append_event(trade["tradeId"], {
        "kind": "fill",
        "payload": {"qty": 100, "price": 1680.0, "complete": True},
    })
    assert holding["status"] == "持仓中"

    closed = trading.append_event(trade["tradeId"], {
        "kind": "close",
        "payload": {"price": 1700.0},
    })
    assert closed["status"] == "已平仓"
    account = read_accounts(tmp_path)["accounts"][0]
    assert account["capital"] == 502000.0
    assert len(account["settlements"]) == 1

    # 相同 close 原请求重试只补/确认 settlement，不重复事件和资金结转。
    retried = trading.append_event(trade["tradeId"], {
        "kind": "close",
        "payload": {"price": 1700.0},
    })
    assert retried["status"] == "已平仓"
    account = read_accounts(tmp_path)["accounts"][0]
    assert account["capital"] == 502000.0
    assert len(account["settlements"]) == 1
    assert len([e for e in store.read_events(tmp_path, trade["tradeId"]) if e["kind"] == "close"]) == 1
