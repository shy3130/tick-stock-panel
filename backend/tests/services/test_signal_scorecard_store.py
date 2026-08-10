"""Signal Scorecard 存储层测试 — append-only 去重 / 过滤 / 并发 / 损坏行 fail-soft。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from app.services import signal_scorecard_store as store


def _ev(eid: str = "signal_ma_golden_5_20_600519.SH_2026-08-04", **kw) -> dict:
    base = {
        "id": eid,
        "signal_key": "signal_ma_golden_5_20",
        "signal_name": "MA5上穿MA20",
        "signal_kind": "builtin",
        "source": "pipeline",
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "date": "2026-08-04",
        "anchor_price": 1680.0,
        "direction_expected": "up",
        "created_ts": 1.0,
        "context": {},
    }
    base.update(kw)
    return base


def _oc(eid: str, horizon: int, **kw) -> dict:
    base = {
        "id": f"{eid}_{horizon}_{store.ENGINE_VERSION}",
        "event_id": eid,
        "horizon": horizon,
        "eval_window_days": horizon,
        "engine_version": store.ENGINE_VERSION,
        "eval_status": "completed",
        "outcome": "hit",
        "stock_return_pct": 3.5,
        "evaluated_ts": 2.0,
    }
    base.update(kw)
    return base


# ── append_event 去重 ────────────────────────────────────
def test_append_event_dedup_same_id(tmp_path: Path):
    ev = _ev()
    assert store.append_event(tmp_path, ev) is True
    # 同 id 再次写入 → 幂等, 不重复
    assert store.append_event(tmp_path, ev) is False
    events = store.list_events(tmp_path)
    assert len(events) == 1


def test_append_event_distinct_ids_both_written(tmp_path: Path):
    assert store.append_event(tmp_path, _ev("a_600519.SH_2026-08-04")) is True
    assert store.append_event(tmp_path, _ev("a_000001.SZ_2026-08-04")) is True
    assert len(store.list_events(tmp_path)) == 2


def test_append_event_no_id_rejected(tmp_path: Path):
    assert store.append_event(tmp_path, {"signal_key": "x"}) is False
    assert store.list_events(tmp_path) == []


# ── list_events 过滤 ─────────────────────────────────────
def test_list_events_filters(tmp_path: Path):
    store.append_event(tmp_path, _ev("k_600519.SH_2026-08-01", signal_key="k", symbol="600519.SH", date="2026-08-01"))
    store.append_event(tmp_path, _ev("k_000001.SZ_2026-08-03", signal_key="k", symbol="000001.SZ", date="2026-08-03"))
    store.append_event(tmp_path, _ev("j_600519.SH_2026-08-05", signal_key="j", symbol="600519.SH", date="2026-08-05"))

    assert len(store.list_events(tmp_path, signal_key="k")) == 2
    assert len(store.list_events(tmp_path, symbol="600519.SH")) == 2
    assert len(store.list_events(tmp_path, date_from="2026-08-03")) == 2
    assert len(store.list_events(tmp_path, date_to="2026-08-03")) == 2
    assert len(store.list_events(tmp_path, date_from="2026-08-03", date_to="2026-08-04")) == 1
    # 倒序
    evs = store.list_events(tmp_path)
    assert evs[0]["date"] == "2026-08-05"
    assert evs[-1]["date"] == "2026-08-01"


def test_list_events_limit(tmp_path: Path):
    for i in range(5):
        store.append_event(tmp_path, _ev(f"k_S{i}.SH_2026-08-0{i}", symbol=f"S{i}.SH", date=f"2026-08-0{i}"))
    assert len(store.list_events(tmp_path, limit=2)) == 2


# ── append_outcome 去重 ──────────────────────────────────
def test_append_outcome_dedup_composite_key(tmp_path: Path):
    eid = "a_600519.SH_2026-08-04"
    oc = _oc(eid, 1)
    assert store.append_outcome(tmp_path, oc) is True
    # 同 (event_id, horizon, engine_version) → 幂等
    assert store.append_outcome(tmp_path, oc) is False
    # 不同 horizon → 写入
    assert store.append_outcome(tmp_path, _oc(eid, 3)) is True
    outcomes = store.list_outcomes(tmp_path, event_id=eid)
    assert len(outcomes) == 2


def test_append_outcome_completed_not_overwritten(tmp_path: Path):
    """completed outcome 不可覆盖: 即便传入不同 outcome 值, 同 key 也不写。"""
    eid = "a_600519.SH_2026-08-04"
    store.append_outcome(tmp_path, _oc(eid, 1, outcome="hit", stock_return_pct=3.0))
    # 尝试用同 key 写不同结果 → 被拒
    store.append_outcome(tmp_path, _oc(eid, 1, outcome="miss", stock_return_pct=-3.0))
    oc = store.list_outcomes(tmp_path, event_id=eid, horizon=1)[0]
    assert oc["outcome"] == "hit"
    assert oc["stock_return_pct"] == 3.0


def test_append_outcome_engine_version_isolates(tmp_path: Path):
    eid = "a_600519.SH_2026-08-04"
    store.append_outcome(tmp_path, _oc(eid, 1))
    # 新 engine_version → 不同 key → 写入 (重算口径隔离)
    v2 = "tickflow-signal-v2"
    store.append_outcome(tmp_path, _oc(eid, 1, engine_version=v2))
    assert len(store.list_outcomes(tmp_path, event_id=eid)) == 2
    assert len(store.list_outcomes(tmp_path, event_id=eid, engine_version=v2)) == 1


# ── 事件级 status 派生 ───────────────────────────────────
def test_event_status_pending_until_all_horizons(tmp_path: Path):
    eid = "a_600519.SH_2026-08-04"
    assert store.event_status(tmp_path, eid) == "pending"
    store.append_event(tmp_path, _ev(eid))
    assert store.event_status(tmp_path, eid) == "pending"
    store.append_outcome(tmp_path, _oc(eid, 1))
    store.append_outcome(tmp_path, _oc(eid, 3))
    assert store.event_status(tmp_path, eid) == "pending"
    store.append_outcome(tmp_path, _oc(eid, 5))
    store.append_outcome(tmp_path, _oc(eid, 10))
    assert store.event_status(tmp_path, eid) == "mature"


def test_list_events_status_filter(tmp_path: Path):
    eid = "a_600519.SH_2026-08-04"
    store.append_event(tmp_path, _ev(eid))
    # pending (无 outcome)
    assert len(store.list_events(tmp_path, status="pending")) == 1
    assert len(store.list_events(tmp_path, status="mature")) == 0
    for h in store.HORIZONS:
        store.append_outcome(tmp_path, _oc(eid, h))
    assert len(store.list_events(tmp_path, status="mature")) == 1
    assert len(store.list_events(tmp_path, status="pending")) == 0


# ── 损坏行 fail-soft ─────────────────────────────────────
def test_corrupt_line_skipped(tmp_path: Path):
    p = store.events_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(_ev("good_600519.SH_2026-08-04"), ensure_ascii=False) + "\n"
        + "this is not json\n"
        + json.dumps(_ev("good2_600519.SH_2026-08-04"), ensure_ascii=False) + "\n"
        + "\n"
        + "{ broken json\n",
        encoding="utf-8",
    )
    events = store.list_events(tmp_path)
    assert len(events) == 2
    ids = {e["id"] for e in events}
    assert ids == {"good_600519.SH_2026-08-04", "good2_600519.SH_2026-08-04"}


def test_corrupt_outcome_line_skipped(tmp_path: Path):
    p = store.outcomes_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("garbage\n" + json.dumps(_oc("e1", 1)) + "\n", encoding="utf-8")
    outcomes = store.list_outcomes(tmp_path)
    assert len(outcomes) == 1


# ── 并发安全 ─────────────────────────────────────────────
def test_concurrent_append_event_no_dup(tmp_path: Path):
    """多线程并发 append 同一 id: 只有一行落盘 (锁去重)。"""
    ev = _ev("concurrent_600519.SH_2026-08-04")
    results: list[bool] = []
    results_lock = threading.Lock()

    def worker():
        r = store.append_event(tmp_path, ev)
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 1  # 只有一个 True
    assert len(store.list_events(tmp_path)) == 1


def test_concurrent_append_distinct_events_all_persist(tmp_path: Path):
    results: list[bool] = []
    results_lock = threading.Lock()

    def make_worker(i: int):
        def w():
            r = store.append_event(tmp_path, _ev(f"k_S{i}.SH_2026-08-04", symbol=f"S{i}.SH"))
            with results_lock:
                results.append(r)
        return w

    threads = [threading.Thread(target=make_worker(i)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 30
    assert len(store.list_events(tmp_path)) == 30


# ── 确定性 id ─────────────────────────────────────────────
def test_make_event_id_deterministic():
    assert store.make_event_id("signal_x", "600519.SH", "2026-08-04") == "signal_x_600519.SH_2026-08-04"
    assert store.make_event_id("signal_x", "600519.SH", "2026-08-04") == \
        store.make_event_id("signal_x", "600519.SH", "2026-08-04")


def test_make_outcome_id_deterministic():
    assert store.make_outcome_id("eid", 3) == f"eid_3_{store.ENGINE_VERSION}"
