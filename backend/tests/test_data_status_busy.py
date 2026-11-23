"""同步/预热期不启动统计, 任务完成后在后台刷新并保留 null 契约。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api import data as data_api
from app.services.data_status_cache import DataStatusCache
from app.services.pipeline_jobs import job_store


@pytest.fixture()
def clean_caches(monkeypatch):
    cache = DataStatusCache({**data_api._TABLE_TTLS, "storage": 60, "last_runs": float("inf")})
    monkeypatch.setattr(data_api, "_status_cache", cache)
    yield cache
    thread = cache._thread
    if thread is not None:
        thread.join(2)
        assert not thread.is_alive()


def _read(cache, name, fetch, *, busy=False):
    return cache.snapshot({name: fetch}, is_busy=lambda: busy)[name]


def _finish(cache):
    thread = cache._thread
    if thread is not None:
        thread.join(2)
        assert not thread.is_alive()


def test_busy_serves_stale_without_fetch(clean_caches):
    calls = []

    def fetch():
        calls.append(1)
        return {"rows": 42}

    _read(clean_caches, "daily", fetch)
    _finish(clean_caches)
    data_api.invalidate_data_cache("daily")
    assert _read(clean_caches, "daily", fetch, busy=True) == {"rows": 42}
    assert calls == [1]


def test_busy_without_stale_returns_none_and_skips_fetch(clean_caches):
    def fetch():
        raise AssertionError("busy 时不能触发统计")

    assert _read(clean_caches, "minute", fetch, busy=True) is None
    assert clean_caches._thread is None


def test_not_busy_refetches_after_invalidation(clean_caches):
    calls = []

    def fetch():
        calls.append(1)
        return {"rows": len(calls)}

    _read(clean_caches, "daily", fetch)
    _finish(clean_caches)
    data_api.invalidate_data_cache("daily")
    assert _read(clean_caches, "daily", fetch) == {"rows": 1}
    _finish(clean_caches)
    assert _read(clean_caches, "daily", fetch) == {"rows": 2}
    assert len(calls) == 2


def test_storage_busy_serves_stale(clean_caches):
    _read(clean_caches, "storage", lambda: {"total_size_mb": 42})
    _finish(clean_caches)
    data_api.invalidate_data_cache()
    assert _read(clean_caches, "storage", lambda: {}, busy=True) == {"total_size_mb": 42}


def test_storage_busy_cold_start_returns_none(clean_caches):
    assert _read(clean_caches, "storage", lambda: {}, busy=True) is None


def _req(indicators_ready=True):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(indicators_ready=indicators_ready))
    )


def test_busy_when_indicators_warming(clean_caches):
    assert data_api._data_pipeline_busy(_req(indicators_ready=False)) is True


def test_busy_follows_active_job(monkeypatch, clean_caches):
    monkeypatch.setattr(job_store, "_active_id", None)
    assert data_api._data_pipeline_busy(_req()) is False
    monkeypatch.setattr(job_store, "_active_id", "abc123")
    monkeypatch.setattr(job_store, "_active_jobs", {"abc123": {"status": "running"}})
    assert data_api._data_pipeline_busy(_req()) is True
    monkeypatch.setattr(job_store, "_active_jobs", {"abc123": {"status": "succeeded"}})
    assert data_api._data_pipeline_busy(_req()) is False
    monkeypatch.setattr(job_store, "_active_jobs", {})
    assert data_api._data_pipeline_busy(_req()) is False


async def test_status_endpoint_busy_skips_aggregates(monkeypatch, clean_caches, tmp_path):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    req = _req(indicators_ready=False)
    req.app.state.repo = repo

    def boom(*_args, **_kwargs):
        raise AssertionError("busy 下不应重算聚合")

    for name in data_api._TABLE_TTLS:
        monkeypatch.setattr(data_api, f"_safe_aggregate_{name}", boom)
    monkeypatch.setattr(data_api, "_compute_storage", boom)
    monkeypatch.setattr(data_api, "_load_last_runs", boom)
    result = await data_api.status(req)
    assert result["indicators_ready"] is False
    assert result["stats_pending"] is True
    assert result["checked_at"]
    assert result["daily"] is None and result["storage"] is None
    assert clean_caches._thread is None


@pytest.mark.parametrize("job_status", ["pending", "running"])
async def test_status_marks_cold_stats_pending_until_refresh_finishes(
    monkeypatch, clean_caches, tmp_path, job_status,
):
    req = _req()
    req.app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(job_store, "active_status", lambda: job_status)
    calls = []

    def aggregate(_repo):
        calls.append(1)
        return {"trading_days": 240}

    for name in data_api._TABLE_TTLS:
        monkeypatch.setattr(data_api, f"_safe_aggregate_{name}", lambda _repo: None)
    monkeypatch.setattr(data_api, "_safe_aggregate_daily", aggregate)
    monkeypatch.setattr(data_api, "_safe_aggregate_enriched", aggregate)
    busy = await data_api.status(req)
    assert busy["indicators_ready"] is True
    assert busy["stats_pending"] is True
    assert busy["daily"] is None and busy["enriched"] is None
    assert calls == []

    monkeypatch.setattr(job_store, "active_status", lambda: "succeeded")
    refreshing = await data_api.status(req)
    assert refreshing["stats_pending"] is True
    _finish(clean_caches)
    ready = await data_api.status(req)
    assert ready["stats_pending"] is False
    assert ready["daily"]["trading_days"] == 240
    assert ready["enriched"]["trading_days"] == 240
    assert len(calls) == 2
