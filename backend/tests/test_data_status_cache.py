"""后台统计的去重、原子发布、TTL 和失效竞态回归。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services import data_status_cache
from app.services.data_status_cache import DataStatusCache


def finish(cache):
    thread = cache._thread
    if thread is not None:
        thread.join(2)
        assert not thread.is_alive()


def read(cache, loaders, is_busy=lambda: False):
    return cache.snapshot(loaders, is_busy=is_busy)


def test_concurrent_requests_share_one_background_refresh():
    cache = DataStatusCache({"daily": 30, "storage": 60})
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow():
        calls.append(threading.current_thread())
        entered.set()
        assert release.wait(3)
        return {"rows": 42}

    loaders = {"daily": slow, "storage": lambda: {"total_size_mb": 1}}
    try:
        with ThreadPoolExecutor(8) as pool:
            responses = list(pool.map(lambda _: read(cache, loaders), range(8)))
        assert entered.wait(1)
        assert all(result["daily"] is None and result["stats_pending"] for result in responses)
        assert len(calls) == 1
        assert calls[0].daemon
        assert calls[0] is not threading.current_thread()
    finally:
        release.set()
        finish(cache)
    assert read(cache, loaders) == {
        "daily": {"rows": 42}, "storage": {"total_size_mb": 1}, "stats_pending": False,
    }
    assert len(calls) == 1


def test_refresh_keeps_old_snapshot_until_whole_batch_is_ready():
    cache = DataStatusCache({"daily": 30, "storage": 60})
    read(cache, {"daily": lambda: {"rows": 1}, "storage": lambda: {"total_size_mb": 1}})
    finish(cache)
    cache.invalidate()
    entered, release = threading.Event(), threading.Event()

    def storage():
        entered.set()
        assert release.wait(3)
        return {"total_size_mb": 2}

    loaders = {"daily": lambda: {"rows": 2}, "storage": storage}
    try:
        result = read(cache, loaders)
        assert result["daily"] == {"rows": 1}
        assert entered.wait(1)
        result = read(cache, loaders)
        assert result == {
            "daily": {"rows": 1}, "storage": {"total_size_mb": 1}, "stats_pending": True,
        }
    finally:
        release.set()
        finish(cache)
    assert read(cache, loaders) == {
        "daily": {"rows": 2}, "storage": {"total_size_mb": 2}, "stats_pending": False,
    }


@pytest.mark.parametrize("value", [None, {"rows": 0}])
def test_empty_results_obey_ttl_and_ttl_starts_after_refresh(monkeypatch, value):
    clock = [100.0]
    monkeypatch.setattr(data_status_cache.time, "monotonic", lambda: clock[0])
    cache = DataStatusCache({"daily": 30})
    calls = []

    def fetch():
        calls.append(1)
        clock[0] += 40  # 模拟一次超过 TTL 的查询。
        return value

    loaders = {"daily": fetch}
    read(cache, loaders)
    finish(cache)
    for _ in range(5):
        assert read(cache, loaders) == {"daily": value, "stats_pending": False}
    assert len(calls) == 1
    clock[0] += 30
    assert read(cache, loaders)["stats_pending"] is True
    finish(cache)
    assert len(calls) == 2


@pytest.mark.parametrize("invalidate_all", [False, True])
def test_invalidation_during_refresh_cannot_publish_old_result(invalidate_all):
    cache = DataStatusCache({"daily": 30, "storage": 60})
    entered, release = threading.Event(), threading.Event()

    def old_daily():
        entered.set()
        assert release.wait(3)
        return {"rows": 99}

    loaders = {"daily": old_daily, "storage": lambda: {"total_size_mb": 99}}
    try:
        read(cache, loaders)
        assert entered.wait(1)
        cache.invalidate(None if invalidate_all else "daily")
    finally:
        release.set()
        finish(cache)
    result = read(cache, loaders, is_busy=lambda: True)
    assert result["daily"] is None
    assert result["storage"] == (None if invalidate_all else {"total_size_mb": 99})
    read(cache, {"daily": lambda: {"rows": 1}, "storage": lambda: {"total_size_mb": 1}})
    finish(cache)
    result = read(cache, loaders)
    assert result["daily"] == {"rows": 1}
    assert result["stats_pending"] is False


def test_discard_stale_removes_old_counts_until_empty_snapshot_is_ready():
    cache = DataStatusCache({"daily": 30})
    read(cache, {"daily": lambda: {"rows": 42}})
    finish(cache)
    cache.invalidate(discard_stale=True)
    assert read(cache, {"daily": lambda: None}, is_busy=lambda: True) == {
        "daily": None, "stats_pending": True,
    }
    read(cache, {"daily": lambda: None})
    finish(cache)
    assert read(cache, {"daily": lambda: None}) == {"daily": None, "stats_pending": False}


def test_fine_grained_invalidation_leaves_storage_and_other_tables_cached():
    cache = DataStatusCache({"daily": 30, "minute": 120, "storage": 60})
    calls = []

    def fetch(key):
        calls.append(key)
        return {"rows": calls.count(key)}

    loaders = {key: lambda key=key: fetch(key) for key in ("daily", "minute", "storage")}
    read(cache, loaders)
    finish(cache)
    cache.invalidate("daily")
    read(cache, loaders)
    finish(cache)
    assert calls == ["daily", "minute", "storage", "daily"]


def test_worker_stops_starting_queries_if_pipeline_becomes_busy():
    cache = DataStatusCache({"daily": 30, "storage": 60})
    busy = [False]
    calls = []

    def daily():
        busy[0] = True
        return {"rows": 1}

    loaders = {"daily": daily, "storage": lambda: calls.append("storage")}
    read(cache, loaders, is_busy=lambda: busy[0])
    finish(cache)
    assert calls == []
    busy[0] = False
    read(cache, loaders, is_busy=lambda: busy[0])
    finish(cache)
    assert calls == ["storage"]


def test_failed_refresh_keeps_old_value_and_allows_retry(caplog):
    cache = DataStatusCache({"daily": 30})
    read(cache, {"daily": lambda: {"rows": 1}})
    finish(cache)
    cache.invalidate()

    def fail():
        raise OSError("injected scan failure")

    read(cache, {"daily": fail})
    finish(cache)
    assert "injected scan failure" in caplog.text
    result = read(cache, {"daily": lambda: {"rows": 2}})
    assert result == {"daily": {"rows": 1}, "stats_pending": True}
    finish(cache)
    assert read(cache, {"daily": fail}) == {"daily": {"rows": 2}, "stats_pending": False}


def test_thread_start_failure_does_not_break_status_or_prevent_retry(monkeypatch, caplog):
    cache = DataStatusCache({"daily": 30})
    loaders = {"daily": lambda: {"rows": 1}}
    with monkeypatch.context() as patch:
        def fail_start(_thread):
            raise RuntimeError("injected thread failure")

        patch.setattr(threading.Thread, "start", fail_start)
        assert read(cache, loaders) == {"daily": None, "stats_pending": True}
    assert cache._thread is None
    assert "injected thread failure" in caplog.text
    read(cache, loaders)
    finish(cache)
    assert read(cache, loaders)["stats_pending"] is False
