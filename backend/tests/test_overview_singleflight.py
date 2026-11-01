import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from app.api import overview


@pytest.fixture(autouse=True)
def reset_cache():
    overview.invalidate_overview_cache()
    yield
    overview.invalidate_overview_cache()


def test_concurrent_cold_requests_share_one_build(monkeypatch):
    barrier = threading.Barrier(8)
    calls = []

    def build(*args):
        calls.append(1)
        time.sleep(0.08)
        return {"rows": [{"symbol": "600000.SH", "change_pct": 0.05}]}

    def request():
        barrier.wait(timeout=2)
        return overview.market_overview(None)

    monkeypatch.setattr(overview, "_build_overview", build)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: request(), range(8)))
    assert len(calls) == 1
    assert all(result == results[0] for result in results)


def test_invalidation_during_build_never_repopulates_old_cache(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def build(*args):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
            return {"value": "old"}
        return {"value": "new"}

    monkeypatch.setattr(overview, "_build_overview", build)
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(overview.market_overview, None)
        assert entered.wait(2)
        overview.invalidate_overview_cache()
        assert overview.market_overview(None) == {"value": "new"}
        release.set()
        pending.result(timeout=2)
    assert overview.market_overview(None) == {"value": "new"}


def test_failed_build_releases_followers_and_can_retry(monkeypatch):
    barrier = threading.Barrier(4)
    calls = []

    def fail(*args):
        calls.append(1)
        time.sleep(0.08)
        raise ValueError("unavailable")

    def request():
        barrier.wait(timeout=2)
        with pytest.raises(ValueError, match="unavailable"):
            overview.market_overview(None)

    monkeypatch.setattr(overview, "_build_overview", fail)
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: request(), range(4)))
    assert len(calls) == 1
    monkeypatch.setattr(overview, "_build_overview", lambda *args: {"ok": True})
    assert overview.market_overview(None) == {"ok": True}


def test_different_dates_build_independently(monkeypatch):
    barrier = threading.Barrier(2)

    def build(request, as_of):
        barrier.wait(timeout=2)
        return {"date": as_of}

    monkeypatch.setattr(overview, "_build_overview", build)
    days = [date(2026, 10, 28), date(2026, 10, 29)]
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(lambda day: overview.market_overview(None, day), days)) == [{"date": d} for d in days]


def test_slow_build_does_not_extend_existing_freshness_window(monkeypatch):
    now, calls = [100.0], []
    monkeypatch.setattr(overview.time, "monotonic", lambda: now[0])

    def build(*args):
        calls.append(1)
        now[0] += 2.0
        return {"build": len(calls)}

    monkeypatch.setattr(overview, "_build_overview", build)
    assert overview.market_overview(None) == {"build": 1}
    now[0] = 104.0
    assert overview.market_overview(None) == {"build": 1}
    now[0] = 105.0
    assert overview.market_overview(None) == {"build": 2}
