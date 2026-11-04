import gc
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.api import backtest as api
from app.services.shared_work import WorkBusyError


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(api, "_running_jobs", {})
    monkeypatch.setattr(api, "_daily_data_revisions", lambda: {})


def test_active_limit_keeps_reconnects_available(monkeypatch):
    monkeypatch.setattr(api, "_MAX_ACTIVE_JOBS", 1)
    job, new = api._acquire_or_evict_job("a", force=False)
    assert new
    assert api._acquire_or_evict_job("a", force=False) == (job, False)
    with pytest.raises(WorkBusyError):
        api._acquire_or_evict_job("b", force=False)


def test_only_one_connection_can_start_an_unstarted_job():
    job, _ = api._acquire_or_evict_job("a", force=False)
    with ThreadPoolExecutor(20) as pool:
        results = list(pool.map(lambda _: api._claim_job_start(job), range(50)))
    assert sum(results) == 1


def test_completed_cache_eviction_does_not_retain_results_via_timer(monkeypatch):
    timers = []

    class Timer:
        def __init__(self, delay, callback):
            self.callback = callback
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(api.threading, "Timer", Timer)
    monkeypatch.setattr(api, "_MAX_FINISHED_JOBS", 1)
    first, _ = api._acquire_or_evict_job("a", force=False)
    reference = weakref.ref(first)
    api._finish_job(first, result={"large": [1] * 1000})
    second, _ = api._acquire_or_evict_job("b", force=False)
    api._finish_job(second, result={})
    api._cleanup_stale_jobs()
    assert set(api._running_jobs) == {"b"}
    assert sum(not timer.cancelled for timer in timers) == 1
    del first
    gc.collect()
    assert reference() is None
