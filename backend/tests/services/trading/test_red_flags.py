"""机械红旗检测器测试 — 构造事件流覆盖三条红旗 + 正常流程零误报。"""
from __future__ import annotations

from app.services.trading.red_flags import scan_all, scan_trade, scan_trade_events

TS = "2026-08-04 14:30"
TS2 = "2026-08-04 15:00"
TS3 = "2026-08-04 15:30"


def _event(kind: str, ts: str, payload: dict | None = None, **kw) -> dict:
    e = {"schemaVersion": 1, "tradeId": "t1", "kind": kind, "ts": ts,
         "payload": payload or {}, "note": "", "gateBypassed": False}
    e.update(kw)
    return e


def _audit(mode: str, ts: str, passed: bool = True) -> dict:
    return {"schemaVersion": 1, "ts": ts, "mode": mode, "tradeId": "t1",
            "symbol": "600519.SH", "passed": passed, "gates": [], "missing": [], "note": ""}


def _full_events(extra: list[dict] | None = None) -> list[dict]:
    """正常流程: open→prepare→fill→tp→close(零红旗)。"""
    events = [
        _event("open", TS, {"name": "贵州茅台", "stopLoss": 1600.0}),
        _event("prepare", TS, {"plannedQty": 100, "plannedPrice": 1680, "stopLoss": 1600.0}),
        _event("fill", TS, {"qty": 100, "price": 1680.0}),
        _event("tp", TS2, {"qty": 40, "price": 1750.0}),
        _event("close", TS3, {"price": 1720.0}),
    ]
    if extra:
        events.extend(extra)
    return events


def _full_audit() -> list[dict]:
    return [
        _audit("buy_new", TS),
        _audit("fill", TS),
        _audit("tp", TS2),
        _audit("close", TS3),
    ]


# ── 正常流程零红旗 ───────────────────────────────────────
def test_clean_flow_no_flags():
    flags = scan_trade_events(_full_events(), _full_audit())
    assert flags == []


def test_empty_events_no_flags():
    assert scan_trade_events([], []) == []


# ── 放宽止损 ─────────────────────────────────────────────
def test_stop_loss_widened_detected():
    # costPrice=1680, 旧止损=1600(距离 4.76%), 新止损=1520(距离 9.52%) → 放宽
    events = [
        _event("open", TS, {"stopLoss": 1600.0}),
        _event("prepare", TS, {"plannedQty": 100, "plannedPrice": 1680}),
        _event("fill", TS, {"qty": 100, "price": 1680.0}),
        _event("adjust", TS2, {"oldStopLoss": 1600.0, "newStopLoss": 1520.0}),
    ]
    audit = [_audit("buy_new", TS), _audit("fill", TS), _audit("adjust", TS2)]
    flags = scan_trade_events(events, audit)
    widened = [f for f in flags if f["type"] == "stop_loss_widened"]
    assert len(widened) == 1
    assert widened[0]["old"] == 1600.0
    assert widened[0]["new"] == 1520.0
    assert widened[0]["costPrice"] == 1680.0


def test_stop_loss_tightened_not_flagged():
    # 向上抬高/收紧: 旧止损=1600, 新止损=1650(距离缩小) → 不报
    events = [
        _event("open", TS, {"stopLoss": 1600.0}),
        _event("prepare", TS, {"plannedQty": 100, "plannedPrice": 1680}),
        _event("fill", TS, {"qty": 100, "price": 1680.0}),
        _event("adjust", TS2, {"oldStopLoss": 1600.0, "newStopLoss": 1650.0}),
    ]
    audit = [_audit("buy_new", TS), _audit("fill", TS), _audit("adjust", TS2)]
    flags = scan_trade_events(events, audit)
    assert not any(f["type"] == "stop_loss_widened" for f in flags)


def test_stop_loss_adjust_before_fill_not_flagged():
    # costPrice=0 时不检测放宽止损
    events = [
        _event("open", TS, {"stopLoss": 1600.0}),
        _event("adjust", TS2, {"oldStopLoss": 1600.0, "newStopLoss": 1400.0}),
    ]
    flags = scan_trade_events(events, [])
    assert not any(f["type"] == "stop_loss_widened" for f in flags)


# ── 亏损加仓 ─────────────────────────────────────────────
def test_loss_add_detected():
    # costPrice=1680, 加仓价 1650 < 1680 → 红旗
    events = [
        _event("open", TS, {"stopLoss": 1600.0}),
        _event("prepare", TS, {"plannedQty": 100, "plannedPrice": 1680}),
        _event("fill", TS, {"qty": 100, "price": 1680.0}),
        _event("add", TS2, {"qty": 100, "price": 1650.0}),
    ]
    audit = [_audit("buy_new", TS), _audit("fill", TS), _audit("add", TS2)]
    flags = scan_trade_events(events, audit)
    loss_adds = [f for f in flags if f["type"] == "loss_add"]
    assert len(loss_adds) == 1
    assert loss_adds[0]["price"] == 1650.0
    assert loss_adds[0]["costPrice"] == 1680.0


def test_profit_add_not_flagged():
    # 加仓价 1700 > 1680 → 不报
    events = [
        _event("open", TS, {"stopLoss": 1600.0}),
        _event("prepare", TS, {"plannedQty": 100, "plannedPrice": 1680}),
        _event("fill", TS, {"qty": 100, "price": 1680.0}),
        _event("add", TS2, {"qty": 100, "price": 1700.0}),
    ]
    audit = [_audit("buy_new", TS), _audit("fill", TS), _audit("add", TS2)]
    flags = scan_trade_events(events, audit)
    assert not any(f["type"] == "loss_add" for f in flags)


def test_plan_only_add_not_flagged():
    events = [
        _event("open", TS, {"stopLoss": 1600.0}),
        _event("prepare", TS, {"plannedQty": 100, "plannedPrice": 1680}),
        _event("fill", TS, {"qty": 100, "price": 1680.0}),
        _event("add", TS2, {"planOnly": True, "qty": 100, "price": 1000.0}),
    ]
    flags = scan_trade_events(events, [_audit("buy_new", TS), _audit("fill", TS)])
    assert not any(f["type"] == "loss_add" for f in flags)


def test_loss_add_uses_avg_cost_after_multiple_fills():
    # fill 100@1680 → cost=1680; add 100@1600(cost→1640); 再 add@1605<1640 → 红旗
    events = [
        _event("open", TS, {"stopLoss": 1600.0}),
        _event("prepare", TS, {"plannedQty": 100, "plannedPrice": 1680}),
        _event("fill", TS, {"qty": 100, "price": 1680.0}),
        _event("add", TS, {"qty": 100, "price": 1600.0}),
        _event("add", TS2, {"qty": 100, "price": 1605.0}),
    ]
    audit = [_audit("buy_new", TS), _audit("fill", TS), _audit("add", TS), _audit("add", TS2)]
    flags = scan_trade_events(events, audit)
    loss_adds = [f for f in flags if f["type"] == "loss_add"]
    # 1600 < 1680 → 红旗; 1605 < 1640(均价) → 红旗
    assert len(loss_adds) == 2
    assert loss_adds[1]["costPrice"] == 1640.0  # 第二次检测时的成本


# ── 绕过门禁 ─────────────────────────────────────────────
def test_gate_bypassed_detected():
    events = [
        _event("open", TS, {"stopLoss": 1600.0}),
        _event("prepare", TS, {"plannedQty": 100, "plannedPrice": 1680}),
        _event("fill", TS, {"qty": 100, "price": 1680.0}, gateBypassed=True),
    ]
    audit = [_audit("buy_new", TS), _audit("fill", TS)]
    flags = scan_trade_events(events, audit)
    bypassed = [f for f in flags if f["type"] == "gate_bypassed"]
    assert len(bypassed) == 1
    assert bypassed[0]["kind"] == "fill"


# ── 审计缺失 ─────────────────────────────────────────────
def test_audit_missing_detected():
    # fill 事件存在但审计流没有 mode=fill 的记录 → audit_missing
    events = [
        _event("open", TS, {"stopLoss": 1600.0}),
        _event("prepare", TS, {"plannedQty": 100, "plannedPrice": 1680}),
        _event("fill", TS, {"qty": 100, "price": 1680.0}),
    ]
    audit = [_audit("buy_new", TS)]  # 缺 fill 审计
    flags = scan_trade_events(events, audit)
    missing = [f for f in flags if f["type"] == "audit_missing"]
    assert len(missing) == 1
    assert missing[0]["kind"] == "fill"


def test_audit_missing_on_close():
    events = _full_events()
    audit = [_audit("buy_new", TS), _audit("fill", TS), _audit("tp", TS2)]  # 缺 close
    flags = scan_trade_events(events, audit)
    missing = [f for f in flags if f["type"] == "audit_missing"]
    assert len(missing) == 1
    assert missing[0]["kind"] == "close"


# ── 组合:多红旗同时命中 ─────────────────────────────────
def test_multiple_flags_combined():
    events = [
        _event("open", TS, {"stopLoss": 1600.0}),
        _event("prepare", TS, {"plannedQty": 100, "plannedPrice": 1680}),
        _event("fill", TS, {"qty": 100, "price": 1680.0}),
        _event("add", TS, {"qty": 100, "price": 1600.0}),  # 亏损加仓
        _event("adjust", TS2, {"oldStopLoss": 1600.0, "newStopLoss": 1500.0}),  # 放宽止损
    ]
    audit = [_audit("buy_new", TS)]  # 缺 fill/add/adjust 审计
    flags = scan_trade_events(events, audit)
    types = {f["type"] for f in flags}
    assert "loss_add" in types
    assert "stop_loss_widened" in types
    assert "audit_missing" in types


# ── tp/sl 不改变成本价(部分卖出) ─────────────────────────
def test_partial_sell_keeps_cost_for_subsequent_flags():
    # fill 100@1680; tp 40@1750(成本不变 1680); adjust 放宽 → 用 1680 检测
    events = [
        _event("open", TS, {"stopLoss": 1600.0}),
        _event("prepare", TS, {"plannedQty": 100, "plannedPrice": 1680}),
        _event("fill", TS, {"qty": 100, "price": 1680.0}),
        _event("tp", TS2, {"qty": 40, "price": 1750.0}),
        _event("adjust", TS3, {"oldStopLoss": 1600.0, "newStopLoss": 1500.0}),
    ]
    audit = [_audit("buy_new", TS), _audit("fill", TS), _audit("tp", TS2), _audit("adjust", TS3)]
    flags = scan_trade_events(events, audit)
    widened = [f for f in flags if f["type"] == "stop_loss_widened"]
    assert len(widened) == 1
    assert widened[0]["costPrice"] == 1680.0  # 部分卖出后成本不变


# ── scan_trade / scan_all 磁盘集成 ──────────────────────
def test_scan_trade_reads_disk(tmp_path):
    from app.services.trading import store
    from app.services.trading.lifecycle import apply_event, new_trade
    from app.services.trading.models import KIND_ADJUST, KIND_PREPARE, KIND_FILL, KIND_TP

    trade = new_trade("t1", "600519.SH", {"name": "茅台", "thesis": {"text": "x", "invalidation": "y"}, "stopLoss": 1600.0}, TS)
    trade = apply_event(trade, KIND_PREPARE, {"plannedQty": 100, "plannedPrice": 1680, "stopLoss": 1600.0}, TS)
    trade = apply_event(trade, KIND_FILL, {"qty": 100, "price": 1680.0}, TS)
    trade = apply_event(trade, KIND_ADJUST, {"newStopLoss": 1500.0}, TS)

    store.write_trade(tmp_path, trade)
    store.append_event(tmp_path, {"schemaVersion": 1, "tradeId": "t1", "kind": "open", "ts": TS, "payload": {}, "note": "", "gateBypassed": False})
    store.append_event(tmp_path, {"schemaVersion": 1, "tradeId": "t1", "kind": "prepare", "ts": TS, "payload": {}, "note": "", "gateBypassed": False})
    store.append_event(tmp_path, {"schemaVersion": 1, "tradeId": "t1", "kind": "fill", "ts": TS, "payload": {"qty": 100, "price": 1680.0}, "note": "", "gateBypassed": False})
    store.append_event(tmp_path, {"schemaVersion": 1, "tradeId": "t1", "kind": "adjust", "ts": TS2, "payload": {"oldStopLoss": 1600.0, "newStopLoss": 1500.0}, "note": "", "gateBypassed": False})
    store.append_audit(tmp_path, {"ts": TS, "mode": "buy_new", "tradeId": "t1", "passed": True})
    store.append_audit(tmp_path, {"ts": TS, "mode": "fill", "tradeId": "t1", "passed": True})
    store.append_audit(tmp_path, {"ts": TS2, "mode": "adjust", "tradeId": "t1", "passed": True})

    flags = scan_trade(tmp_path, "t1")
    assert any(f["type"] == "stop_loss_widened" for f in flags)

    all_flags = scan_all(tmp_path)
    assert "t1" in all_flags
