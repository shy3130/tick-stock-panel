from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import heavy_job_limiter, pipeline_jobs, preferences
from app.services.heavy_job_limiter import HeavyJobLimiter
from app.services.pipeline_jobs import JobCancelledError, JobStore, run_with_capacity
from app.tickflow.repository import KlineRepository


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


def waiting(store, jid):
    log = store.get(jid)["log"]
    return bool(log) and log[-1]["stage"] == "init"


@pytest.fixture
def capacity(monkeypatch, tmp_path):
    monkeypatch.setattr(preferences, "load", lambda: {})
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    store = JobStore(store_dir=tmp_path / "jobs")
    limiter = HeavyJobLimiter(capacity=2, cancel_poll_interval=0.005)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(heavy_job_limiter, "shared_heavy_job_limiter", limiter)
    return store, limiter


def test_queued_pipeline_is_cancellable_and_reaped_when_stalled(capacity):
    """排队等槽任务: 仍可手动取消; 停滞超阈值后会被 reap 回收, 不再无限空转。

    旧契约「排队任务不被 reap」正是「分钟K同步卡死空转」的排队侧根因:
    槽位被僵尸线程/长回测占住时, 排队任务在「等待其他计算任务完成…」上
    无任何超时兜底, 只能重启进程。
    """
    store, limiter = capacity
    assert limiter.acquire("normal", timeout=0)  # 占一半预算, exclusive 需全量 → 排队
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            # 1) 排队中手动取消 → 协作式退出(limiter cancel_event), fn 不执行
            jid, _ = store.create(timeout_s=3600)
            called = threading.Event()
            future = pool.submit(run_with_capacity, jid, called.set)
            wait_for(lambda: waiting(store, jid))
            assert store.get(jid)["status"] == "pending"
            store.terminate(jid, "cancelled in queue")
            with pytest.raises(JobCancelledError):
                future.result(timeout=1)
            assert not called.is_set()

            # 2) 排队停滞超阈值 → reap 回收(修复点), 等待线程经取消标志退出
            jid2, _ = store.create(timeout_s=3600)
            called2 = threading.Event()
            future2 = pool.submit(run_with_capacity, jid2, called2.set)
            wait_for(lambda: waiting(store, jid2))
            stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
            store._active_jobs[jid2]["started_at"] = stale
            store._active_jobs[jid2]["last_progress_at"] = stale
            store.reap_stale()
            j2 = store.get(jid2)
            assert j2["status"] == "failed"
            assert "排队等待重任务执行槽" in j2["error"]
            with pytest.raises(JobCancelledError):
                future2.result(timeout=1)
            assert not called2.is_set()
            assert limiter.in_use == 1
    finally:
        limiter.release("normal")
    assert limiter.in_use == 0


def test_cancel_does_not_release_running_worker_capacity(capacity):
    store, limiter = capacity
    jid, _ = store.create()
    entered, finish = threading.Event(), threading.Event()

    def work():
        entered.set()
        assert finish.wait(2)
        raise JobCancelledError(jid)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_with_capacity, jid, work)
        try:
            assert entered.wait(1)
            store.terminate(jid, "cancelled while computing")
            assert store.get(jid)["status"] == "failed"
            assert limiter.in_use == 2
            assert not limiter.acquire("normal", timeout=0)
        finally:
            finish.set()
        with pytest.raises(JobCancelledError):
            future.result(timeout=1)
    assert limiter.in_use == 0


def test_cache_refresh_queues_and_can_nest_in_pipeline(capacity, monkeypatch):
    store, limiter = capacity
    repo = object.__new__(KlineRepository)
    calls = []
    monkeypatch.setattr(repo, "_refresh_enriched_impl", lambda: calls.append(limiter.in_use))
    assert limiter.acquire("normal", timeout=0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(repo._refresh_enriched)
        try:
            wait_for(lambda: bool(limiter._waiters))
            assert calls == []
        finally:
            limiter.release("normal")
        future.result(timeout=1)
    jid, _ = store.create()
    run_with_capacity(jid, repo._refresh_enriched)
    assert calls == [2, 2]
    assert limiter.in_use == 0


@pytest.mark.parametrize("kind", ["signal", "factor", "factor_batch"])
def test_in_process_backtests_share_capacity(capacity, monkeypatch, kind):
    from app.backtest.factor import FactorBacktestService
    from app.services.backtest import BacktestService

    _, limiter = capacity
    cls = BacktestService if kind == "signal" else FactorBacktestService
    service = object.__new__(cls)
    method = "run_batch" if kind == "factor_batch" else "run"
    received = []
    config = object()

    def compute(actual, **kwargs):
        assert actual is config
        assert limiter.in_use == 2
        received.append(True)
        return "unchanged result"

    monkeypatch.setattr(service, "_" + method, compute)
    assert limiter.acquire("normal", timeout=0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(getattr(service, method), config)
        try:
            wait_for(lambda: bool(limiter._waiters))
            assert received == []
        finally:
            limiter.release("normal")
        assert future.result(timeout=1) == "unchanged result"
    # Auto-mining evaluates factors within its existing two-slot reservation.
    with limiter.slot("mining"):
        assert getattr(service, method)(config) == "unchanged result"
    assert limiter.in_use == 0


@pytest.mark.parametrize("endpoint,body,module_name,function_name,result", [
    ("/api/pipeline/run", {}, "app.jobs.daily_pipeline", "run_now", {}),
    ("/api/kline/extend_history", {"value": 1, "unit": "month"},
     "app.services.extend_history", "run_extend_history", {}),
    ("/api/kline/repair_daily", {"start_date": "2026-01-01"},
     "app.services.repair_daily", "run_repair_daily", {}),
    ("/api/kline/rebuild_enriched", {}, "app.indicators.pipeline", "run_pipeline", 3),
    ("/api/kline/sync_minute", {"days": 1},
     "app.services.kline_sync", "sync_and_persist_minute", 3),
])
def test_api_jobs_wait_before_computing(
    capacity, monkeypatch, tmp_path, endpoint, body, module_name, function_name, result,
):
    import importlib

    from app.api import kline, pipeline
    from app.jobs import daily_pipeline

    store, limiter = capacity
    entered, finish = threading.Event(), threading.Event()

    def compute(*args, **kwargs):
        assert limiter.in_use == 2
        entered.set()
        assert finish.wait(3)
        return result

    monkeypatch.setattr(importlib.import_module(module_name), function_name, compute)
    monkeypatch.setattr(pipeline, "job_store", store)
    monkeypatch.setattr("app.tickflow.pools.get_pool", lambda *args: [])
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda *args: None)
    app = FastAPI()
    app.include_router(pipeline.router)
    app.include_router(kline.router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        db=SimpleNamespace(execute=lambda *args: None),
        refresh_cache=lambda: None,
        get_index_symbol_set=lambda: set(),
    )
    app.state.capabilities = SimpleNamespace(has=lambda _: True)
    assert limiter.acquire("normal", timeout=0)
    released = False
    with TestClient(app) as client:
        try:
            response = client.post(endpoint, json=body)
            assert response.status_code == 200, response.text
            jid = response.json()["job_id"]
            wait_for(lambda: waiting(store, jid))
            assert not entered.is_set()
            assert client.get(f"/api/pipeline/jobs/{jid}").json()["status"] == "pending"
            limiter.release("normal")
            released = True
            assert entered.wait(2)
            assert not limiter.acquire("normal", timeout=0)
            finish.set()
            wait_for(lambda: store.get(jid)["status"] in ("succeeded", "failed"))
            assert store.get(jid)["status"] == "succeeded", store.get(jid)
        finally:
            finish.set()
            if not released:
                limiter.release("normal")
    wait_for(lambda: limiter.in_use == 0)


@pytest.mark.parametrize("fail", [False, True])
def test_manual_pipeline_holds_capacity_through_refresh_off_event_loop(capacity, monkeypatch, fail):
    from app.api import pipeline

    store, limiter = capacity
    refreshing, finish = threading.Event(), threading.Event()

    def compute(*args, **kwargs):
        if fail:
            raise ValueError("partial pipeline failure")
        return {"rows": 3}

    def refresh():
        assert limiter.in_use == 2
        refreshing.set()
        assert finish.wait(3)

    monkeypatch.setattr(pipeline, "job_store", store)
    monkeypatch.setattr(pipeline.daily_pipeline, "run_now", compute)
    app = FastAPI()
    app.include_router(pipeline.router)
    app.state.repo = SimpleNamespace(refresh_cache=refresh)
    app.state.capabilities = object()
    with TestClient(app) as client:
        try:
            jid = client.post("/api/pipeline/run").json()["job_id"]
            assert refreshing.wait(1)
            assert not limiter.acquire("normal", timeout=0)
            # This request must complete while the worker is blocked in refresh.
            assert client.get(f"/api/pipeline/jobs/{jid}").json()["status"] == "running"
            finish.set()
            wait_for(lambda: store.get(jid)["status"] in ("succeeded", "failed"))
            assert store.get(jid)["status"] == ("failed" if fail else "succeeded")
        finally:
            finish.set()
    assert limiter.in_use == 0


@pytest.mark.parametrize("entry", ["scheduled", "integrity"])
def test_background_pipeline_entries_share_capacity(capacity, monkeypatch, entry):
    from app.jobs import daily_pipeline
    from app.services import data_integrity, repair_daily

    store, limiter = capacity
    called = threading.Event()

    def compute(*args, **kwargs):
        assert limiter.in_use == 2
        called.set()
        return {}

    monkeypatch.setattr(repair_daily, "run_repair_daily", compute)
    assert limiter.acquire("normal", timeout=0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        if entry == "scheduled":
            future = pool.submit(daily_pipeline._run_tracked, compute, "test")
        else:
            state = SimpleNamespace(repo=object(), capabilities=SimpleNamespace(has=lambda _: True))
            data_integrity.launch_integrity_repair(state, date(2026, 1, 1), "test")
            future = None
        try:
            wait_for(lambda: store.active_id() is not None)
            jid = store.active_id()
            wait_for(lambda: waiting(store, jid))
            assert store.get(jid)["status"] == "pending"
            assert not called.is_set()
        finally:
            limiter.release("normal")
        wait_for(lambda: store.get(jid)["status"] == "succeeded")
        if future is not None:
            assert future.result(timeout=1)
    assert called.is_set()
    assert limiter.in_use == 0
