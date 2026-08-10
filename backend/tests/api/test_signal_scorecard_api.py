"""Signal Scorecard API 测试 — stats 聚合 / events 过滤 / evaluate 幂等 / backfill 白名单。

直接调用 router 函数 (SimpleNamespace 模拟 request), 与 test_research_schedule 同模式。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from pathlib import Path

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import signal_scorecard as api
from app.services import preferences, signal_scorecard_store as store


def _request(tmp_path: Path, repo=None):
    if repo is None:
        repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def _list_events(req, **kw):
    """直接调用路由函数时, Query(None) 默认值不会自动解析, 需显式传 None。"""
    defaults = dict(signal_key=None, symbol=None, date_from=None,
                    date_to=None, status=None, limit=500)
    defaults.update(kw)
    return api.list_events(req, **defaults)


def _get_stats(req, **kw):
    defaults = dict(signal_key=None, horizon=None, date_from=None, date_to=None)
    defaults.update(kw)
    return api.get_stats(req, **defaults)


def _seed_event(tmp_path, eid="signal_ma_golden_5_20_600519.SH_2026-08-04", **kw):
    ev = {
        "id": eid, "signal_key": "signal_ma_golden_5_20", "signal_name": "MA金叉",
        "signal_kind": "builtin", "source": "pipeline", "symbol": "600519.SH",
        "name": "贵州茅台", "date": "2026-08-04", "anchor_price": 100.0,
        "direction_expected": "up", "created_ts": 1.0, "context": {},
    }
    ev.update(kw)
    store.append_event(tmp_path, ev)
    return ev


def _seed_outcome(tmp_path, eid, horizon, **kw):
    oc = {
        "id": f"{eid}_{horizon}_{store.ENGINE_VERSION}", "event_id": eid,
        "horizon": horizon, "eval_window_days": horizon,
        "engine_version": store.ENGINE_VERSION, "eval_status": "completed",
        "outcome": "hit", "stock_return_pct": 3.0, "end_close": 103.0,
        "evaluated_ts": 2.0, "direction_correct": True, "unable_reason": None,
    }
    oc.update(kw)
    store.append_outcome(tmp_path, oc)
    return oc

# ── GET/PUT /tracked-signals ─────────────────────────────
def test_tracked_signals_get_and_update(monkeypatch):
    stored = [{
        "signal_key": "signal_a",
        "signal_name": "A",
        "signal_kind": "builtin",
        "direction": "up",
        "enabled": True,
    }]
    monkeypatch.setattr(preferences, "get_tracked_signals", lambda: stored)
    assert api.get_tracked_signals() == {"items": stored}

    captured = {}

    def _set(items):
        captured["items"] = items
        return items

    monkeypatch.setattr(preferences, "set_tracked_signals", _set)
    req = api.TrackedSignalsRequest(items=[
        api.TrackedSignalItem(signal_key="signal_a", signal_name="A"),
    ])
    out = api.update_tracked_signals(req)
    assert out["items"][0]["direction"] == "up"
    assert captured["items"][0]["enabled"] is True


# ── GET /events ──────────────────────────────────────────
def test_events_list_and_filter(tmp_path):
    _seed_event(tmp_path, "k_600519.SH_2026-08-01", signal_key="k", symbol="600519.SH", date="2026-08-01")
    _seed_event(tmp_path, "k_000001.SZ_2026-08-03", signal_key="k", symbol="000001.SZ", date="2026-08-03")
    req = _request(tmp_path)
    out = _list_events(req)
    assert out["total"] == 2
    out2 = _list_events(req, symbol="600519.SH")
    assert out2["total"] == 1


def test_events_bad_status_400(tmp_path):
    req = _request(tmp_path)
    with pytest.raises(HTTPException) as exc:
        _list_events(req, status="bogus")
    assert exc.value.status_code == 400


# ── GET /stats 聚合 ───────────────────────────────────────
def test_stats_aggregation_hit_rate(tmp_path):
    """2 个事件, T+1: 1 hit + 1 miss → hit_rate 50%。pending 不计入分母。"""
    e1 = "k_600519.SH_2026-08-04"
    e2 = "k_000001.SZ_2026-08-04"
    _seed_event(tmp_path, e1, signal_key="k", symbol="600519.SH", date="2026-08-04")
    _seed_event(tmp_path, e2, signal_key="k", symbol="000001.SZ", date="2026-08-04")
    _seed_outcome(tmp_path, e1, 1, outcome="hit", stock_return_pct=5.0)
    _seed_outcome(tmp_path, e2, 1, outcome="miss", stock_return_pct=-5.0)
    # e1 还有 T+3 但无 outcome (pending)
    req = _request(tmp_path)
    out = _get_stats(req, signal_key="k", horizon=1)
    row = out["stats"][0]
    assert row["signal_key"] == "k"
    assert row["horizon"] == 1
    assert row["total"] == 2
    assert row["completed"] == 2
    assert row["hit_count"] == 1
    assert row["miss_count"] == 1
    assert row["hit_rate_pct"] == 50.0
    assert row["sample_size"] == 2
    assert row["avg_return_pct"] == 0.0  # (5 + -5)/2


def test_stats_pending_excluded_from_hit_rate(tmp_path):
    """pending (无 outcome) 不计入 hit_rate 分母, 但计入 total。"""
    e1 = "k_600519.SH_2026-08-04"
    e2 = "k_000001.SZ_2026-08-04"
    _seed_event(tmp_path, e1, signal_key="k", symbol="600519.SH")
    _seed_event(tmp_path, e2, signal_key="k", symbol="000001.SZ")
    _seed_outcome(tmp_path, e1, 1, outcome="hit", stock_return_pct=3.0)
    # e2 无 outcome → pending
    req = _request(tmp_path)
    row = _get_stats(req, signal_key="k", horizon=1)["stats"][0]
    assert row["total"] == 2
    assert row["completed"] == 1
    assert row["pending"] == 1
    assert row["hit_count"] == 1
    assert row["hit_rate_pct"] == 100.0  # 分母=completed=1


def test_stats_bad_horizon_400(tmp_path):
    req = _request(tmp_path)
    with pytest.raises(HTTPException) as exc:
        _get_stats(req, horizon=7)
    assert exc.value.status_code == 400


def test_stats_multi_horizon(tmp_path):
    e1 = "k_600519.SH_2026-08-04"
    _seed_event(tmp_path, e1, signal_key="k", symbol="600519.SH")
    _seed_outcome(tmp_path, e1, 1, outcome="hit", stock_return_pct=3.0)
    _seed_outcome(tmp_path, e1, 3, outcome="miss", stock_return_pct=-3.0)
    req = _request(tmp_path)
    out = _get_stats(req, signal_key="k")
    assert len(out["stats"]) == 4  # 4 个 horizon 各一行
    h1 = next(s for s in out["stats"] if s["horizon"] == 1)
    assert h1["hit_count"] == 1
    h3 = next(s for s in out["stats"] if s["horizon"] == 3)
    assert h3["miss_count"] == 1
    h5 = next(s for s in out["stats"] if s["horizon"] == 5)
    assert h5["completed"] == 0  # pending


# ── GET /events/{id}/outcomes ────────────────────────────
def test_event_detail_with_horizons(tmp_path):
    e1 = "k_600519.SH_2026-08-04"
    _seed_event(tmp_path, e1)
    _seed_outcome(tmp_path, e1, 1, outcome="hit")
    req = _request(tmp_path)
    out = api.event_detail(e1, req)
    assert out["event"]["id"] == e1
    assert out["status"] == "pending"
    horizons = {o["horizon"]: o for o in out["outcomes"]}
    assert horizons[1]["outcome"] == "hit"
    assert horizons[3]["eval_status"] == "pending"


def test_event_detail_not_found_404(tmp_path):
    req = _request(tmp_path)
    with pytest.raises(HTTPException) as exc:
        api.event_detail("missing", req)
    assert exc.value.status_code == 404


# ── POST /evaluate 幂等 ──────────────────────────────────
class _FakeRepo:
    """模拟 repo: get_enriched_range 返回前向 OHLC。"""

    def __init__(self, data_dir: Path):
        self.store = SimpleNamespace(data_dir=data_dir)

    def get_enriched_range(self, start, end, symbols=None, columns=None):
        # 返回锚定日之后的 10 个交易日 (足够所有 horizon)
        rows = []
        for i in range(1, 11):
            d = date(2026, 8, 4 + i)  # 8/5 .. 8/14
            close = 110.0 if i == 1 else 100.0  # T+1 close=110 → +10% vs anchor=100 → hit
            rows.append({"symbol": "600519.SH", "date": d, "open": 100.0,
                         "high": close, "low": 99.0, "close": close})
        return pl.DataFrame(rows)


def test_evaluate_idempotent(tmp_path, monkeypatch):
    e1 = "signal_ma_golden_5_20_600519.SH_2026-08-04"
    _seed_event(tmp_path, e1, signal_key="signal_ma_golden_5_20")
    repo = _FakeRepo(tmp_path)
    req = _request(tmp_path, repo=repo)

    r1 = api.evaluate(req)
    assert r1["ok"] is True
    assert r1["appended_outcomes"] == 4  # 4 个 horizon 全到期
    # 幂等: 重复调用不再写
    r2 = api.evaluate(req)
    assert r2["appended_outcomes"] == 0
    outcomes = store.list_outcomes(tmp_path, event_id=e1)
    assert len(outcomes) == 4  # 仍然只有 4 行


def test_evaluate_keeps_pending_when_insufficient(tmp_path):
    """前向交易日不足的 horizon 保持 pending, 不写 unable。"""
    e1 = "signal_ma_golden_5_20_600519.SH_2026-08-04"
    _seed_event(tmp_path, e1, signal_key="signal_ma_golden_5_20")

    class _ShortRepo:
        store = SimpleNamespace(data_dir=tmp_path)

        def get_enriched_range(self, start, end, symbols=None, columns=None):
            # 只返回 2 个交易日 (T+1, T+2)
            return pl.DataFrame([
                {"symbol": "600519.SH", "date": date(2026, 8, 5), "open": 100.0,
                 "high": 110.0, "low": 99.0, "close": 110.0},
                {"symbol": "600519.SH", "date": date(2026, 8, 6), "open": 110.0,
                 "high": 111.0, "low": 109.0, "close": 111.0},
            ])

    req = _request(tmp_path, repo=_ShortRepo())
    r = api.evaluate(req)
    # 只有 horizon=1 到期 (2 天 ≥ 1), horizon 3/5/10 不足 → pending
    assert r["appended_outcomes"] == 1
    outcomes = store.list_outcomes(tmp_path, event_id=e1)
    assert len(outcomes) == 1
    assert outcomes[0]["horizon"] == 1
    assert store.event_status(tmp_path, e1) == "pending"


# ── POST /backfill 白名单 ────────────────────────────────
def test_backfill_rejects_untracked_signal_key(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "get_tracked_signals",
                        lambda: [{"signal_key": "signal_a", "signal_kind": "builtin"}])
    req = _request(tmp_path)
    with pytest.raises(HTTPException) as exc:
        api.backfill(req, signal_keys="signal_a,signal_evil",
                     date_from="2026-08-01", date_to="2026-08-04")
    assert exc.value.status_code == 400
    assert "signal_evil" in str(exc.value.detail)


def test_backfill_rejects_when_no_tracked(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "get_tracked_signals", lambda: [])
    req = _request(tmp_path)
    with pytest.raises(HTTPException) as exc:
        api.backfill(req, signal_keys="signal_a", date_from="2026-08-01", date_to="2026-08-04")
    assert exc.value.status_code == 400


def test_backfill_rejects_bad_date(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "get_tracked_signals",
                        lambda: [{"signal_key": "signal_a", "signal_kind": "builtin"}])
    req = _request(tmp_path)
    with pytest.raises(HTTPException) as exc:
        api.backfill(req, signal_keys="signal_a", date_from="bad", date_to="2026-08-04")
    assert exc.value.status_code == 400


def test_backfill_rejects_inverted_range(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "get_tracked_signals",
                        lambda: [{"signal_key": "signal_a", "signal_kind": "builtin"}])
    req = _request(tmp_path)
    with pytest.raises(HTTPException) as exc:
        api.backfill(req, signal_keys="signal_a", date_from="2026-08-10", date_to="2026-08-01")
    assert exc.value.status_code == 400


def test_backfill_rejects_range_over_400_days(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "get_tracked_signals",
                        lambda: [{"signal_key": "signal_a", "signal_kind": "builtin"}])
    req = _request(tmp_path)
    with pytest.raises(HTTPException) as exc:
        api.backfill(req, signal_keys="signal_a", date_from="2025-01-01", date_to="2026-08-01")
    assert exc.value.status_code == 400


def test_backfill_generates_instances_in_range(tmp_path, monkeypatch):
    """白名单内 + 有效范围 → 逐日扫描生成实例。"""
    monkeypatch.setattr(preferences, "get_tracked_signals",
                        lambda: [{"signal_key": "signal_a", "signal_kind": "builtin",
                                  "signal_name": "A", "enabled": True}])
    # 模拟 enriched: 8/4 和 8/5 有 signal_a=True
    class _BackfillRepo:
        store = SimpleNamespace(data_dir=tmp_path)

        def get_enriched_range(self, start, end, symbols=None, columns=None):
            if start == end:  # generate_instances 单日查询
                if start in (date(2026, 8, 4), date(2026, 8, 5)):
                    return pl.DataFrame([{
                        "symbol": "600519.SH", "date": start, "close": 100.0,
                        "name": "茅台", "signal_a": True,
                    }])
                return pl.DataFrame()
            # evaluate_pending 前向查询
            return pl.DataFrame()

    req = _request(tmp_path, repo=_BackfillRepo())
    r = api.backfill(req, signal_keys="signal_a", date_from="2026-08-04", date_to="2026-08-05")
    assert r["ok"] is True
    assert r["generated"] == 2  # 两天各一个事件
    events = store.list_events(tmp_path)
    assert len(events) == 2


def test_backfill_rejects_disabled_tracked_signal(tmp_path, monkeypatch):
    """已配置但 enabled=False 的 signal_key 必须与未配置一样被拒绝, 且不调用 job.backfill。"""
    monkeypatch.setattr(preferences, "get_tracked_signals", lambda: [
        {"signal_key": "signal_on", "signal_kind": "builtin", "enabled": True},
        {"signal_key": "signal_off", "signal_kind": "builtin", "enabled": False},
    ])
    called = {}
    def _boom(*a, **k):
        called["yes"] = True
        return {"ok": True, "days_scanned": 0}
    monkeypatch.setattr(api.job, "backfill", _boom)
    req = _request(tmp_path)
    with pytest.raises(HTTPException) as exc:
        api.backfill(req, signal_keys="signal_off",
                     date_from="2026-08-01", date_to="2026-08-04")
    assert exc.value.status_code == 400
    assert "signal_off" in str(exc.value.detail)
    assert called == {}  # job.backfill 未被调用 → 无 append-only 事件写入