"""交易计划台测试 — CRUD + deviation 三分类。"""
from __future__ import annotations

import json

import pytest

from app.services.trading import store
from app.services.trading.plans import deviation, read_plan, write_plan

TS = "2026-08-04 14:30"
DATE = "20260804"


# ── CRUD ─────────────────────────────────────────────────
def test_read_plan_absent_returns_none(tmp_path):
    assert read_plan(tmp_path, DATE) is None


def test_write_plan_creates_new(tmp_path):
    plan = write_plan(tmp_path, DATE, {
        "entries": [{
            "id": "p1", "symbol": "600519.SH", "action": "buy_new",
            "trigger": "突破年线", "qty": 100, "reason": "放量突破",
        }],
        "actualNotes": "观察盘面",
    })
    assert plan["date"] == DATE
    assert plan["entries"][0]["symbol"] == "600519.SH"
    assert plan["entries"][0]["createdAt"]
    # 落盘可读回
    read_back = read_plan(tmp_path, DATE)
    assert read_back is not None
    assert read_back["entries"][0]["id"] == "p1"


def test_write_plan_appends_new_entries(tmp_path):
    write_plan(tmp_path, DATE, {"entries": [
        {"id": "p1", "symbol": "600519.SH", "action": "buy_new", "trigger": "t1"},
    ]})
    write_plan(tmp_path, DATE, {"entries": [
        {"id": "p2", "symbol": "000001.SZ", "action": "add", "trigger": "t2"},
    ]})
    plan = read_plan(tmp_path, DATE)
    assert len(plan["entries"]) == 2
    ids = {e["id"] for e in plan["entries"]}
    assert ids == {"p1", "p2"}


def test_write_plan_updates_existing_entry_by_id(tmp_path):
    write_plan(tmp_path, DATE, {"entries": [
        {"id": "p1", "symbol": "600519.SH", "action": "buy_new", "trigger": "t1"},
    ]})
    # 同 id 不同内容 → 更新
    write_plan(tmp_path, DATE, {"entries": [
        {"id": "p1", "symbol": "600519.SH", "action": "buy_new", "trigger": "t2", "reason": "改了理由"},
    ]})
    plan = read_plan(tmp_path, DATE)
    assert len(plan["entries"]) == 1
    assert plan["entries"][0]["trigger"] == "t2"
    assert plan["entries"][0]["reason"] == "改了理由"
    # createdAt 保持首次写入的时间
    assert plan["entries"][0]["createdAt"]


def test_write_plan_preserves_existing_entries_when_partial_submit(tmp_path):
    """前端只提交新增条目时,已有条目不丢失。"""
    write_plan(tmp_path, DATE, {"entries": [
        {"id": "p1", "symbol": "A.SH", "action": "buy_new", "trigger": "t1"},
    ]})
    write_plan(tmp_path, DATE, {"entries": [
        {"id": "p2", "symbol": "B.SH", "action": "add", "trigger": "t2"},
    ]})
    plan = read_plan(tmp_path, DATE)
    assert len(plan["entries"]) == 2


def test_write_plan_replace_removes_omitted_entries(tmp_path):
    write_plan(tmp_path, DATE, {"entries": [
        {"id": "p1", "symbol": "A.SH", "action": "buy_new", "trigger": "t1"},
        {"id": "p2", "symbol": "B.SH", "action": "add", "trigger": "t2"},
    ]})
    plan = write_plan(tmp_path, DATE, {
        "replace": True,
        "entries": [
            {"id": "p2", "symbol": "B.SH", "action": "add", "trigger": "t2"},
        ],
    })
    assert [entry["id"] for entry in plan["entries"]] == ["p2"]


def test_write_plan_validates_action(tmp_path):
    with pytest.raises(ValueError, match="action"):
        write_plan(tmp_path, DATE, {"entries": [
            {"id": "p1", "symbol": "A.SH", "action": "invalid", "trigger": "t"},
        ]})


def test_write_plan_requires_symbol(tmp_path):
    with pytest.raises(ValueError, match="symbol"):
        write_plan(tmp_path, DATE, {"entries": [
            {"id": "p1", "symbol": "", "action": "buy_new", "trigger": "t"},
        ]})


def test_write_plan_requires_id(tmp_path):
    with pytest.raises(ValueError, match="id"):
        write_plan(tmp_path, DATE, {"entries": [
            {"symbol": "A.SH", "action": "buy_new", "trigger": "t"},
        ]})


def test_write_plan_rejects_duplicate_id(tmp_path):
    with pytest.raises(ValueError, match="重复"):
        write_plan(tmp_path, DATE, {"entries": [
            {"id": "p1", "symbol": "A.SH", "action": "buy_new", "trigger": "t"},
            {"id": "p1", "symbol": "B.SH", "action": "add", "trigger": "t"},
        ]})


def test_write_plan_rejects_bad_entries_field(tmp_path):
    with pytest.raises(ValueError, match="entries"):
        write_plan(tmp_path, DATE, {"entries": "not_a_list"})


def test_watch_action_allowed(tmp_path):
    plan = write_plan(tmp_path, DATE, {"entries": [
        {"id": "p1", "symbol": "600519.SH", "action": "watch", "trigger": "观察"},
    ]})
    assert plan["entries"][0]["action"] == "watch"


def test_actual_notes_preserved_on_update(tmp_path):
    write_plan(tmp_path, DATE, {"entries": [{"id": "p1", "symbol": "A.SH", "action": "buy_new", "trigger": "t"}], "actualNotes": "盘前笔记"})
    write_plan(tmp_path, DATE, {"entries": [{"id": "p2", "symbol": "B.SH", "action": "add", "trigger": "t"}]})
    plan = read_plan(tmp_path, DATE)
    assert plan["actualNotes"] == "盘前笔记"


# ── deviation ───────────────────────────────────────────
def _trade_file(tmp_path, trade_id, symbol):
    store.write_trade(tmp_path, {
        "tradeId": trade_id, "symbol": symbol, "name": symbol,
        "status": "持仓中", "position": {"qty": 100, "costPrice": 1680.0, "invested": 168000.0},
        "realizedPnl": 0.0, "stopLoss": 1600.0,
        "thesis": {"text": "x", "invalidation": "y"}, "createdAt": TS, "closedAt": None,
    })


def _event(trade_id, kind, ts, payload=None):
    return {"schemaVersion": 1, "tradeId": trade_id, "kind": kind, "ts": ts,
            "payload": payload or {}, "note": "", "gateBypassed": False}


def test_deviation_matched_when_planned_and_done(tmp_path):
    write_plan(tmp_path, DATE, {"entries": [
        {"id": "p1", "symbol": "600519.SH", "action": "tp", "trigger": "到价止盈"},
    ]})
    _trade_file(tmp_path, "600519.SH_1", "600519.SH")
    store.append_event(tmp_path, _event("600519.SH_1", "tp", TS, {"qty": 50, "price": 1750.0}))
    dev = deviation(tmp_path, DATE)
    assert len(dev["matched"]) == 1
    assert dev["planned_but_not_done"] == []
    assert dev["done_but_not_planned"] == []
    assert dev["plannedCount"] == 1
    assert dev["doneCount"] == 1


def test_deviation_planned_but_not_done(tmp_path):
    write_plan(tmp_path, DATE, {"entries": [
        {"id": "p1", "symbol": "600519.SH", "action": "tp", "trigger": "到价"},
        {"id": "p2", "symbol": "000001.SZ", "action": "buy_new", "trigger": "突破"},
    ]})
    _trade_file(tmp_path, "600519.SH_1", "600519.SH")
    store.append_event(tmp_path, _event("600519.SH_1", "tp", TS, {"qty": 50, "price": 1750.0}))
    # 只执行了 600519 的 tp, 000001 未执行
    dev = deviation(tmp_path, DATE)
    assert len(dev["matched"]) == 1
    assert len(dev["planned_but_not_done"]) == 1
    assert dev["planned_but_not_done"][0]["symbol"] == "000001.SZ"
    assert dev["done_but_not_planned"] == []


def test_deviation_done_but_not_planned(tmp_path):
    write_plan(tmp_path, DATE, {"entries": [
        {"id": "p1", "symbol": "600519.SH", "action": "tp", "trigger": "到价"},
    ]})
    _trade_file(tmp_path, "600519.SH_1", "600519.SH")
    _trade_file(tmp_path, "000001.SZ_1", "000001.SZ")
    store.append_event(tmp_path, _event("600519.SH_1", "tp", TS, {"qty": 50, "price": 1750.0}))
    store.append_event(tmp_path, _event("000001.SZ_1", "add", TS, {"qty": 100, "price": 10.0}))
    dev = deviation(tmp_path, DATE)
    assert len(dev["matched"]) == 1
    assert len(dev["done_but_not_planned"]) == 1
    assert dev["done_but_not_planned"][0]["symbol"] == "000001.SZ"


def test_deviation_buy_new_matches_open_and_fill(tmp_path):
    write_plan(tmp_path, DATE, {"entries": [
        {"id": "p1", "symbol": "600519.SH", "action": "buy_new", "trigger": "突破"},
    ]})
    _trade_file(tmp_path, "600519.SH_1", "600519.SH")
    store.append_event(tmp_path, _event("600519.SH_1", "open", TS, {"name": "茅台", "stopLoss": 1600}))
    dev = deviation(tmp_path, DATE)
    assert len(dev["matched"]) == 1
    assert dev["done_but_not_planned"] == []


def test_deviation_watch_not_counted_as_planned_action(tmp_path):
    write_plan(tmp_path, DATE, {"entries": [
        {"id": "p1", "symbol": "600519.SH", "action": "watch", "trigger": "观察"},
    ]})
    _trade_file(tmp_path, "600519.SH_1", "600519.SH")
    store.append_event(tmp_path, _event("600519.SH_1", "tp", TS, {"qty": 50, "price": 1750.0}))
    dev = deviation(tmp_path, DATE)
    assert dev["plannedCount"] == 0  # watch 不计入
    assert len(dev["done_but_not_planned"]) == 1


def test_deviation_empty_plan_and_events(tmp_path):
    dev = deviation(tmp_path, DATE)
    assert dev["plannedCount"] == 0
    assert dev["doneCount"] == 0
    assert dev["matched"] == []
    assert dev["planned_but_not_done"] == []
    assert dev["done_but_not_planned"] == []


def test_deviation_filters_events_by_date(tmp_path):
    write_plan(tmp_path, DATE, {"entries": [
        {"id": "p1", "symbol": "600519.SH", "action": "tp", "trigger": "到价"},
    ]})
    _trade_file(tmp_path, "600519.SH_1", "600519.SH")
    # 前一天的事件不算
    store.append_event(tmp_path, _event("600519.SH_1", "tp", "2026-08-03 14:30", {"qty": 50, "price": 1750.0}))
    dev = deviation(tmp_path, DATE)
    assert len(dev["planned_but_not_done"]) == 1
    assert dev["doneCount"] == 0


def test_deviation_case_insensitive_symbol_matching(tmp_path):
    write_plan(tmp_path, DATE, {"entries": [
        {"id": "p1", "symbol": "600519.SH", "action": "tp", "trigger": "到价"},
    ]})
    _trade_file(tmp_path, "600519.SH_1", "600519.sh")  # 小写
    store.append_event(tmp_path, _event("600519.SH_1", "tp", TS, {"qty": 50, "price": 1750.0}))
    dev = deviation(tmp_path, DATE)
    assert len(dev["matched"]) == 1


def test_deviation_prepare_revise_excluded_from_done(tmp_path):
    """prepare/revise 是建仓准备, 不算实际交易动作。"""
    write_plan(tmp_path, DATE, {"entries": []})
    _trade_file(tmp_path, "600519.SH_1", "600519.SH")
    store.append_event(tmp_path, _event("600519.SH_1", "prepare", TS, {"plannedQty": 100, "plannedPrice": 1680}))
    dev = deviation(tmp_path, DATE)
    assert dev["doneCount"] == 0
    assert dev["done_but_not_planned"] == []
