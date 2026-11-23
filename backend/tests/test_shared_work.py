import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.shared_work import SharedWork, WorkBusyError


def test_fifty_cold_requests_execute_once():
    work = SharedWork("strategy.single")
    barrier = threading.Barrier(50)
    calls = []

    def request(_):
        barrier.wait(5)
        return work.run("same", lambda: calls.append(1) or {"value": 42}, ttl_s=1)

    with ThreadPoolExecutor(50) as pool:
        results = list(pool.map(request, range(50)))
    assert calls == [1]
    assert all(result == {"value": 42} for result in results)


def test_overlapping_batches_only_fetch_unclaimed_keys():
    work = SharedWork("minute.pull")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fetch(keys):
        calls.append(keys)
        if "a" in keys:
            entered.set()
            assert release.wait(3)
        else:
            release.set()
        return {key: key.upper() for key in keys}

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(work.run_many, ["a", "b"], fetch, ttl_s=1)
        assert entered.wait(3)
        second = pool.submit(work.run_many, ["b", "c"], fetch, ttl_s=1)
        assert first.result(3) == {"a": "A", "b": "B"}
        assert second.result(3) == {"b": "B", "c": "C"}
    assert calls == [["a", "b"], ["c"]]


def test_manual_rerun_bypasses_completed_cache_but_keeps_identity():
    work = SharedWork("strategy.single")
    calls = []
    def run():
        return work.run("k", lambda: len(calls.append(1) or calls), ttl_s=5)
    assert run() == run() == 1
    assert work.run("k", lambda: 2, ttl_s=0) == 2
    assert run() == 2  # manual computation evicts the previous cached value


def test_failure_releases_followers_and_can_retry():
    work = SharedWork("minute.pull")
    entered, release = threading.Event(), threading.Event()

    def fail():
        entered.set()
        assert release.wait(3)
        raise ValueError("upstream unavailable")

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(work.run, "k", fail)
        assert entered.wait(3)
        second = pool.submit(work.run, "k", fail)
        release.set()
        for future in (first, second):
            with pytest.raises(ValueError, match="upstream unavailable"):
                future.result(3)
    assert work.run("k", lambda: 3) == 3


def test_pending_limit_does_not_reject_followers_or_leak_claims():
    work = SharedWork("minute.pull", max_pending=1)
    entered, release = threading.Event(), threading.Event()

    def fetch():
        entered.set()
        assert release.wait(3)
        return 1

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(work.run, "a", fetch)
        assert entered.wait(3)
        with pytest.raises(WorkBusyError):
            work.run("b", lambda: 2)
        follower = pool.submit(work.run, "a", fetch)
        release.set()
        assert first.result(3) == follower.result(3) == 1
    assert work.run("b", lambda: 2) == 2


def test_clear_during_build_prevents_old_cache_repopulation():
    work = SharedWork("strategy.single")

    def build():
        work.clear()
        return "old"

    assert work.run("k", build, ttl_s=5) == "old"
    assert work.run("k", lambda: "new", ttl_s=5) == "new"


def test_cache_is_bounded_by_entries_and_bytes(monkeypatch):
    work = SharedWork("strategy.single", max_entries=2, max_bytes=4, size_of=len)
    for key in ("a", "b", "c"):
        work.run(key, lambda: "xx", ttl_s=1)
    assert work.cache_info() == {"entries": 2, "bytes": 4, "pending": 0}
    work.run("large", lambda: "oversized", ttl_s=1)
    assert work.cache_info()["bytes"] <= 4


def test_automatic_failure_has_short_cooldown_without_caching_exceptions(monkeypatch):
    from app.services import shared_work

    now = [100.0]
    monkeypatch.setattr(shared_work.time, "monotonic", lambda: now[0])
    work = SharedWork("minute.pull")

    def fail():
        raise ValueError("temporary upstream error")

    with pytest.raises(ValueError):
        work.run("a", fail, ttl_s=2)
    with pytest.raises(WorkBusyError):
        work.run("a", lambda: 1, ttl_s=2)
    now[0] += 1.1
    assert work.run("a", lambda: 1, ttl_s=2) == 1
