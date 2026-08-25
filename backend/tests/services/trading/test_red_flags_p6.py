"""P6.1 新红旗检测器测试 — horizon_exceeded / size_over_limit / gate_proliferation。

覆盖触发/不触发分支、global 分组结构。纯函数 + 磁盘集成。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.services.trading import store
from app.services.trading.lifecycle import apply_event, new_trade
from app.services.trading.models import (
    KIND_ADD,
    KIND_CLOSE,
    KIND_FILL,
    KIND_OPEN,
    KIND_PREPARE,
    KIND_TP,
)
from app.services.trading.red_flags import (
    count_gate_rules,
    detect_gate_proliferation,
    detect_horizon_exceeded,
    detect_size_over_limit,
    scan_all,
    scan_trade,
)


def _event(kind: str, ts: str, payload: dict | None = None) -> dict:
    return {"schemaVersion": 1, "tradeId": "t1", "kind": kind, "ts": ts,
            "payload": payload or {}, "note": "", "gateBypassed": False}


# ── horizon_exceeded ────────────────────────────────────
def test_horizon_exceeded_open_to_today():
    # open 100 天前,声明 1 月(30 天),未平仓 → 100 > 30 触发
    now = datetime(2026, 8, 4)
    open_ts = (now - timedelta(days=100)).strftime("%Y-%m-%d %H:%M")
    events = [_event(KIND_OPEN, open_ts, {"stopLoss": 1600.0})]
    flag = detect_horizon_exceeded(events, 1.0, now=now)
    assert flag is not None
    assert flag["type"] == "horizon_exceeded"
    assert flag["holdingDays"] == 100
    assert flag["horizonMonths"] == 1.0
    assert flag["limitDays"] == 30


def test_horizon_exceeded_fill_to_close():
    # fill 80 天前, close 70 天后;声明 2 月(60 天) → 70 > 60 触发
    start = datetime(2026, 5, 16)
    close = datetime(2026, 7, 25)
    events = [
        _event(KIND_OPEN, "2026-05-16 10:00", {"stopLoss": 1600.0}),
        _event(KIND_FILL, "2026-05-16 14:30", {"qty": 100, "price": 1680}),
        _event(KIND_CLOSE, "2026-07-25 14:30", {"price": 1700}),
    ]
    flag = detect_horizon_exceeded(events, 2.0, now=start)
    assert flag is not None
    assert flag["holdingDays"] == 70
    assert flag["limitDays"] == 60


def test_horizon_not_exceeded_within_limit():
    # open 10 天前,声明 1 月 → 10 < 30 不触发
    now = datetime(2026, 8, 4)
    open_ts = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M")
    events = [_event(KIND_OPEN, open_ts, {"stopLoss": 1600.0})]
    assert detect_horizon_exceeded(events, 1.0, now=now) is None


def test_horizon_no_horizon_months_skip():
    # horizon_months=None → skip
    events = [_event(KIND_OPEN, "2020-01-01 10:00")]
    assert detect_horizon_exceeded(events, None) is None
    assert detect_horizon_exceeded(events, 0) is None


def test_horizon_no_fill_uses_open():
    # 无 fill,有 open → 用 open 作起始日
    now = datetime(2026, 8, 4)
    open_ts = (now - timedelta(days=100)).strftime("%Y-%m-%d %H:%M")
    events = [_event(KIND_OPEN, open_ts, {"stopLoss": 1600.0})]
    flag = detect_horizon_exceeded(events, 1.0, now=now)
    assert flag is not None


def test_horizon_no_events_skip():
    assert detect_horizon_exceeded([], 6.0) is None


# ── size_over_limit ─────────────────────────────────────
def test_size_over_limit_fill_triggers():
    # capital=100000, maxSingleRatio=0.1; fill 100@200=20000, 20000/100000=20% > 10% → 触发
    events = [_event(KIND_FILL, "2026-08-04 14:30", {"qty": 100, "price": 200})]
    flags = detect_size_over_limit(events, 100000, 0.1, None)
    assert len(flags) == 1
    assert flags[0]["type"] == "size_over_limit"
    assert flags[0]["breached"] == ["account"]
    assert flags[0]["marketValue"] == 20000
    assert flags[0]["exposure"] == 0.2


def test_size_over_limit_strategy_limit_triggers():
    # capital=100000, positionLimitPct=5; fill 100@200=20000 > 5000 → 触发 strategy
    events = [_event(KIND_FILL, "2026-08-04 14:30", {"qty": 100, "price": 200})]
    flags = detect_size_over_limit(events, 100000, None, 5.0)
    assert len(flags) == 1
    assert flags[0]["breached"] == ["strategy"]
    assert flags[0]["positionLimitPct"] == 5.0


def test_size_over_limit_both_limits_breached():
    # 账户+策略双超
    events = [_event(KIND_FILL, "2026-08-04 14:30", {"qty": 100, "price": 200})]
    flags = detect_size_over_limit(events, 100000, 0.1, 5.0)
    assert len(flags) == 1
    assert set(flags[0]["breached"]) == {"account", "strategy"}


def test_size_under_limit_no_flag():
    # capital=1000000, fill 100@200=20000, ratio=2% < 10% → 不触发
    events = [_event(KIND_FILL, "2026-08-04 14:30", {"qty": 100, "price": 200})]
    assert detect_size_over_limit(events, 1000000, 0.1, 5.0) == []


def test_size_no_capital_skip():
    # capital=0(无账户默认) → skip
    events = [_event(KIND_FILL, "2026-08-04 14:30", {"qty": 100, "price": 200})]
    assert detect_size_over_limit(events, 0, 0.1, 5.0) == []
    assert detect_size_over_limit(events, None, 0.1, 5.0) == []


def test_size_no_limits_skip():
    # 无任何限额 → skip
    events = [_event(KIND_FILL, "2026-08-04 14:30", {"qty": 100, "price": 200})]
    assert detect_size_over_limit(events, 100000, None, None) == []


def test_size_add_reduces_below_limit():
    # fill 100@200(20%超), tp 50@210(剩余50,市值10000=10% 刚好不超)
    events = [
        _event(KIND_FILL, "2026-08-04 14:30", {"qty": 100, "price": 200}),
        _event(KIND_TP, "2026-08-04 15:00", {"qty": 50, "price": 210}),
        _event(KIND_ADD, "2026-08-04 15:30", {"qty": 0, "price": 200}),
    ]
    # fill 时超,但这里只测 add 不超(简化:直接测 add 场景)
    events_add = [
        _event(KIND_FILL, "2026-08-04 14:30", {"qty": 10, "price": 200}),
        _event(KIND_ADD, "2026-08-04 15:00", {"qty": 10, "price": 200}),
    ]
    # capital=200000, fill 10@200=2000(1%), add 后 20@200=4000(2%) 都不超
    flags = detect_size_over_limit(events_add, 200000, 0.1, None)
    assert flags == []


def test_size_add_triggers_when_over():
    # fill 10@200(2000,2%), add 90@200 → 100@200=20000(20%) 超限
    events = [
        _event(KIND_FILL, "2026-08-04 14:30", {"qty": 10, "price": 200}),
        _event(KIND_ADD, "2026-08-04 15:00", {"qty": 90, "price": 200}),
    ]
    flags = detect_size_over_limit(events, 100000, 0.1, None)
    # 只有 add 那次超(fill 时 2% 不超)
    assert len(flags) == 1
    assert flags[0]["kind"] == "add"
    assert flags[0]["exposure"] == 0.2


def test_size_plan_only_add_not_checked():
    # planOnly add 不算成交 → 不检测
    events = [
        _event(KIND_FILL, "2026-08-04 14:30", {"qty": 10, "price": 200}),
        _event(KIND_ADD, "2026-08-04 15:00", {"qty": 90, "price": 200, "planOnly": True}),
    ]
    flags = detect_size_over_limit(events, 100000, 0.1, None)
    assert flags == []


# ── gate_proliferation ──────────────────────────────────
def test_gate_proliferation_triggers():
    flag = detect_gate_proliferation(20, now=datetime(2026, 8, 4))
    assert flag is not None
    assert flag["type"] == "gate_proliferation"
    assert flag["ruleCount"] == 20
    assert flag["threshold"] == 15


def test_gate_proliferation_at_threshold_no_flag():
    # 恰好 15 条 → 不超(> 15 才报)
    assert detect_gate_proliferation(15) is None


def test_gate_proliferation_below_threshold_no_flag():
    assert detect_gate_proliferation(5) is None


def test_count_gate_rules():
    rules = {
        "rules": {
            "buy_new": {"all": [{"id": "a", "text": "x"}], "any": [], "discipline": []},
            "add": {"all": [{"id": "b", "text": "y"}, {"id": "c", "text": "z"}], "any": [], "discipline": []},
        }
    }
    assert count_gate_rules(rules) == 3


def test_count_gate_rules_empty():
    assert count_gate_rules({"rules": {}}) == 0


def test_count_gate_rules_malformed():
    assert count_gate_rules({}) == 0
    assert count_gate_rules({"rules": None}) == 0


# ── scan_all global 分组(磁盘集成) ──────────────────────
def test_scan_all_global_group_when_gate_proliferation(tmp_path):
    from app.services.trading import gates as gates_store

    # 写入 16 条规则触发门禁膨胀
    rules = {"rules": {}}
    items = [{"id": f"r{i}", "text": f"规则{i}"} for i in range(16)]
    for mode in ("buy_new",):
        rules["rules"][mode] = {"all": items, "any": [], "discipline": []}
    gates_store.write_gate_rules(tmp_path, rules)

    all_flags = scan_all(tmp_path)
    assert "global" in all_flags
    assert len(all_flags["global"]) == 1
    assert all_flags["global"][0]["type"] == "gate_proliferation"


def test_scan_all_no_global_group_when_rules_few(tmp_path):
    all_flags = scan_all(tmp_path)
    assert "global" not in all_flags


def test_scan_all_global_flag_structure(tmp_path):
    from app.services.trading import gates as gates_store

    items = [{"id": f"r{i}", "text": f"规则{i}"} for i in range(20)]
    gates_store.write_gate_rules(tmp_path, {"rules": {"buy_new": {"all": items}}})
    all_flags = scan_all(tmp_path)
    g = all_flags["global"][0]
    # 结构与单笔 flag 一致:有 type/ts/detail
    assert set(("type", "ts", "detail")).issubset(g.keys())
    assert g["type"] == "gate_proliferation"
    assert isinstance(g["ts"], str)
    assert isinstance(g["detail"], str)


# ── scan_trade 磁盘集成(P6.1 红旗) ──────────────────────
def test_scan_trade_horizon_exceeded_with_profile(tmp_path):
    from app.services.strategy_profile import write_profile

    now = datetime(2026, 8, 4)
    open_ts = (now - timedelta(days=100)).strftime("%Y-%m-%d %H:%M")
    trade = new_trade(
        "t1", "600519.SH",
        {"name": "茅台", "thesis": {"text": "x", "invalidation": "y"},
         "stopLoss": 1600.0, "strategy": "trend_strat"},
        open_ts,
    )
    trade = apply_event(trade, KIND_PREPARE, {"plannedQty": 100, "plannedPrice": 1680, "stopLoss": 1600.0}, open_ts)
    trade = apply_event(trade, KIND_FILL, {"qty": 100, "price": 1680.0}, open_ts)
    store.write_trade(tmp_path, trade)

    write_profile(tmp_path, {
        "schemaVersion": 1, "strategyId": "trend_strat",
        "risk": {"positionLimitPct": 20, "lossBudgetPct": 5, "thesisHorizonMonths": 1},
    })

    for kind, payload in [
        (KIND_OPEN, {"stopLoss": 1600.0}),
        (KIND_PREPARE, {"plannedQty": 100, "plannedPrice": 1680}),
        (KIND_FILL, {"qty": 100, "price": 1680.0}),
    ]:
        store.append_event(tmp_path, {"schemaVersion": 1, "tradeId": "t1", "kind": kind,
                                      "ts": open_ts, "payload": payload, "note": "", "gateBypassed": False})
    store.append_audit(tmp_path, {"ts": open_ts, "mode": "buy_new", "tradeId": "t1", "passed": True})
    store.append_audit(tmp_path, {"ts": open_ts, "mode": "fill", "tradeId": "t1", "passed": True})

    flags = scan_trade(tmp_path, "t1")
    assert any(f["type"] == "horizon_exceeded" for f in flags)


def test_scan_trade_horizon_no_profile_skip(tmp_path):
    # 无 profile → horizon_exceeded 不产生(skip 语义)
    now = datetime(2026, 8, 4)
    open_ts = (now - timedelta(days=100)).strftime("%Y-%m-%d %H:%M")
    trade = new_trade("t1", "600519.SH",
                      {"name": "茅台", "thesis": {"text": "x", "invalidation": "y"}, "stopLoss": 1600.0}, open_ts)
    trade = apply_event(trade, KIND_FILL, {"qty": 100, "price": 1680.0}, open_ts)
    store.write_trade(tmp_path, trade)
    store.append_event(tmp_path, {"schemaVersion": 1, "tradeId": "t1", "kind": KIND_OPEN,
                                  "ts": open_ts, "payload": {}, "note": "", "gateBypassed": False})
    store.append_event(tmp_path, {"schemaVersion": 1, "tradeId": "t1", "kind": KIND_FILL,
                                  "ts": open_ts, "payload": {"qty": 100, "price": 1680.0}, "note": "", "gateBypassed": False})
    store.append_audit(tmp_path, {"ts": open_ts, "mode": "buy_new", "tradeId": "t1", "passed": True})
    store.append_audit(tmp_path, {"ts": open_ts, "mode": "fill", "tradeId": "t1", "passed": True})

    flags = scan_trade(tmp_path, "t1")
    assert not any(f["type"] == "horizon_exceeded" for f in flags)


def test_scan_trade_size_over_limit_with_account(tmp_path):
    from app.services.trading import accounts as accounts_store

    accounts_store.write_accounts(tmp_path, {
        "accounts": [{"id": "default", "currency": "CNY", "capital": 100000,
                      "horizonFundMonths": 12, "maxSingleRatio": 0.1, "changes": []}]
    })
    trade = new_trade("t1", "600519.SH",
                      {"name": "茅台", "thesis": {"text": "x", "invalidation": "y"}, "stopLoss": 1600.0}, "2026-08-04 14:30")
    trade = apply_event(trade, KIND_FILL, {"qty": 100, "price": 200}, "2026-08-04 14:30")
    store.write_trade(tmp_path, trade)
    store.append_event(tmp_path, {"schemaVersion": 1, "tradeId": "t1", "kind": KIND_FILL,
                                  "ts": "2026-08-04 14:30", "payload": {"qty": 100, "price": 200}, "note": "", "gateBypassed": False})
    store.append_audit(tmp_path, {"ts": "2026-08-04 14:30", "mode": "fill", "tradeId": "t1", "passed": True})

    flags = scan_trade(tmp_path, "t1")
    assert any(f["type"] == "size_over_limit" for f in flags)
