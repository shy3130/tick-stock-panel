"""状态请求不能等待统计, 并发请求共享同一次后台刷新。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio.to_thread
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import data as data_api
from app.services.data_status_cache import DataStatusCache
from app.services.pipeline_jobs import job_store


@pytest.fixture
def cache(monkeypatch):
    cache = DataStatusCache({**data_api._TABLE_TTLS, "storage": 60, "last_runs": float("inf")})
    monkeypatch.setattr(data_api, "_status_cache", cache)
    yield cache
    thread = cache._thread
    if thread is not None:
        thread.join(4)
        assert not thread.is_alive()


@pytest.mark.parametrize("part", ["daily", "storage", "last_runs"])
def test_cold_status_returns_before_slow_aggregate_finishes(tmp_path, monkeypatch, cache, part):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_aggregate(*_args):
        calls.append(1)
        entered.set()
        release.wait(3)
        return {
            "daily": {"trading_days": 240},
            "storage": {"total_size_mb": 42},
            "last_runs": {"last_pipeline_run": "2026-11-04T06:00:00Z", "last_instruments_run": None},
        }[part]

    for name in data_api._TABLE_TTLS:
        monkeypatch.setattr(data_api, f"_safe_aggregate_{name}", lambda _repo: None)
    target = {"daily": "_safe_aggregate_daily", "storage": "_compute_storage", "last_runs": "_load_last_runs"}
    monkeypatch.setattr(data_api, target[part], slow_aggregate)
    app = FastAPI()
    app.include_router(data_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.indicators_ready = True
    try:
        with TestClient(app) as client, ThreadPoolExecutor(1) as pool:
            response = pool.submit(client.get, "/api/data/status")
            assert entered.wait(1)
            result = response.result(timeout=0.5)
            assert result.status_code == 200
            assert result.json()["stats_pending"] is True
            assert result.json()["daily"] is None
            assert result.json()["storage"] is None
            assert result.json()["last_pipeline_run"] is None
            assert calls == [1]
    finally:
        release.set()


def test_status_does_not_queue_behind_saturated_sync_route_pool(tmp_path, cache):
    entered, release = threading.Event(), threading.Event()

    @asynccontextmanager
    async def lifespan(_app):
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        try:
            yield
        finally:
            limiter.total_tokens = previous

    app = FastAPI(lifespan=lifespan)
    app.include_router(data_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.indicators_ready = False

    @app.get("/slow")
    def slow():
        entered.set()
        release.wait(3)
        return {"ok": True}

    with TestClient(app) as client, ThreadPoolExecutor(2) as pool:
        slow_request = pool.submit(client.get, "/slow")
        try:
            assert entered.wait(1)
            response = pool.submit(client.get, "/api/data/status").result(timeout=0.5)
            assert response.status_code == 200
            assert response.json()["stats_pending"] is True
            assert cache._thread is None
        finally:
            release.set()
            assert slow_request.result(timeout=2).status_code == 200


def test_busy_status_does_not_wait_for_job_persistence_lock(tmp_path, monkeypatch, cache):
    app = FastAPI()
    app.include_router(data_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.indicators_ready = True
    monkeypatch.setattr(job_store, "_active_id", "sync-job")
    monkeypatch.setattr(job_store, "_active_jobs", {"sync-job": {"status": "running"}})
    with TestClient(app) as client, ThreadPoolExecutor(1) as pool:
        job_store._lock.acquire()
        try:
            response = pool.submit(client.get, "/api/data/status").result(timeout=0.5)
            assert response.status_code == 200
            assert response.json()["stats_pending"] is True
            assert cache._thread is None
        finally:
            job_store._lock.release()
