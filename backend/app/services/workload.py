"""Bounded process-local counters; labels never contain request parameters or credentials."""
from __future__ import annotations

import copy
import hashlib
import threading
import time
from collections import OrderedDict, deque

_GROUPS = frozenset({
    "http.screener", "http.kline", "http.overview", "http.watchlist", "http.backtest",
    "strategy.batch", "strategy.unit", "strategy.single", "screener.custom", "minute.pull", "minute.single", "minute.sync",
    "daily.prepare", "backtest.task", "market.calendar",
})
_EVENTS = frozenset({"request", "executed", "completed", "shared", "cache_hit", "rejected", "failed"})


class WorkloadStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._groups: dict[str, dict[str, float | int]] = {}

    def record(self, group: str, event: str, *, elapsed_s: float = 0) -> None:
        if group not in _GROUPS or event not in _EVENTS:
            return
        milliseconds = max(0.0, elapsed_s * 1000)
        with self._lock:
            entry = self._groups.setdefault(group, {})
            entry[event] = entry.get(event, 0) + 1
            if elapsed_s:
                entry["total_ms"] = entry.get("total_ms", 0) + milliseconds
                entry["max_ms"] = max(entry.get("max_ms", 0), milliseconds)

    def snapshot(self) -> dict:
        with self._lock:
            return {"uptime_s": time.monotonic() - self._started, "groups": copy.deepcopy(self._groups)}


workload = WorkloadStats()


class SubmissionLimiter:
    """Limit new computations per authenticated session; reads/subscriptions are free."""

    def __init__(self, *, limit=30, window_s=60, max_sessions=1024, clock=time.monotonic):
        self._limit, self._window, self._max_sessions = limit, window_s, max_sessions
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, deque] = OrderedDict()

    def check(self, credential: str | None) -> None:
        if not credential:
            return  # Local password-free deployments keep their existing behavior.
        from app.services.shared_work import WorkBusyError

        key = hashlib.sha256(credential.encode()).hexdigest()
        now = self._clock()
        with self._lock:
            times = self._sessions.setdefault(key, deque())
            self._sessions.move_to_end(key)
            while times and times[0] <= now - self._window:
                times.popleft()
            if len(times) >= self._limit:
                raise WorkBusyError("新建计算任务过于频繁，请稍后重试")
            times.append(now)
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)


submissions = SubmissionLimiter()


def admit_request(request, cookie_name: str) -> None:
    credential = getattr(request, "cookies", {}).get(cookie_name)
    if not credential:
        credential = getattr(request, "headers", {}).get("authorization")
    submissions.check(credential)
