from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.shared_work import WorkBusyError
from app.services.workload import SubmissionLimiter, WorkloadStats


def test_concurrent_events_and_detached_snapshot():
    stats = WorkloadStats()
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda _: stats.record("strategy.batch", "shared"), range(1000)))
    snapshot = stats.snapshot()
    assert snapshot["groups"]["strategy.batch"]["shared"] == 1000
    snapshot["groups"]["strategy.batch"]["shared"] = 0
    assert stats.snapshot()["groups"]["strategy.batch"]["shared"] == 1000


def test_unknown_labels_are_not_retained_and_timing_is_bounded():
    stats = WorkloadStats()
    for index in range(1000):
        stats.record(f"user-{index}", "request")
    stats.record("http.screener", "request", elapsed_s=0.25)
    stats.record("http.screener", "request", elapsed_s=0.5)
    groups = stats.snapshot()["groups"]
    assert list(groups) == ["http.screener"]
    assert groups["http.screener"]["request"] == 2
    assert groups["http.screener"]["total_ms"] == 750
    assert groups["http.screener"]["max_ms"] == 500


def test_submission_budget_expires_and_is_bounded_without_retaining_tokens():
    now = [0.0]
    limiter = SubmissionLimiter(limit=2, max_sessions=2, clock=lambda: now[0])
    limiter.check("secret-session")
    limiter.check("secret-session")
    with pytest.raises(WorkBusyError):
        limiter.check("secret-session")
    now[0] = 61
    limiter.check("secret-session")
    limiter.check("second")
    limiter.check("third")
    assert len(limiter._sessions) == 2
    assert "secret-session" not in repr(limiter._sessions)
