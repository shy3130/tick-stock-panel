"""Weighted process-local limiter for memory-heavy jobs."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar, Literal

HeavyJobKind = Literal["normal", "mining", "exclusive"]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


# 内存准入 (issue #516): 可用内存低于阈值时重任务不进门, 挂起等待恢复,
# 避免叠加任务把整机拖进 swap。0 = 关闭。等待者以 _MEMORY_POLL_S 周期重查
# (内存恢复不会触发 condition notify, 必须轮询)。
_HEAVY_MIN_AVAILABLE_BYTES = _env_int("TICK_HEAVY_MIN_AVAILABLE_BYTES", 1024**3)
_MEMORY_POLL_S = 5.0


def _memory_admission_ok() -> bool:
    if _HEAVY_MIN_AVAILABLE_BYTES <= 0:
        return True
    import psutil

    return psutil.virtual_memory().available >= _HEAVY_MIN_AVAILABLE_BYTES


class HeavyJobLimitTimeoutError(TimeoutError):
    """Raised when a heavy-job slot cannot be acquired before its deadline."""


class HeavyJobCancelledError(RuntimeError):
    """Raised when slot acquisition is cancelled while waiting."""


class HeavyJobLimiter:
    """FIFO weighted capacity; exclusive jobs reserve the entire process budget."""

    _WEIGHTS: ClassVar[dict[HeavyJobKind, int]] = {"normal": 1, "mining": 2}

    def __init__(self, capacity: int = 2, *, cancel_poll_interval: float = 0.05) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if cancel_poll_interval <= 0:
            raise ValueError("cancel_poll_interval must be positive")
        self.capacity = capacity
        self._cancel_poll_interval = cancel_poll_interval
        self._used = 0
        self._acquired = {"normal": 0, "mining": 0, "exclusive": 0}
        self._condition = threading.Condition()
        self._waiters: deque[object] = deque()
        self._local = threading.local()

    @property
    def in_use(self) -> int:
        with self._condition:
            return self._used

    @property
    def available(self) -> int:
        with self._condition:
            return self.capacity - self._used

    def acquire(
        self,
        kind: HeavyJobKind = "normal",
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """Wait for capacity and return ``False`` on cancellation or timeout."""
        weight = self._weight(kind)
        if weight > self.capacity:
            raise ValueError(f"{kind} requires {weight} slots, capacity is {self.capacity}")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")

        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            ticket = object()
            self._waiters.append(ticket)
            try:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        return False
                    if (
                        self._waiters[0] is ticket
                        and self._used + weight <= self.capacity
                        and _memory_admission_ok()
                    ):
                        self._used += weight
                        self._acquired[kind] += 1
                        return True

                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    wait_for = remaining
                    if cancel_event is not None:
                        wait_for = self._cancel_poll_interval
                        if remaining is not None:
                            wait_for = min(wait_for, remaining)
                    # 内存准入需要周期性重查 (内存恢复不触发 notify)
                    if wait_for is None or wait_for > _MEMORY_POLL_S:
                        wait_for = _MEMORY_POLL_S
                    self._condition.wait(wait_for)
            finally:
                self._waiters.remove(ticket)
                self._condition.notify_all()

    def release(self, kind: HeavyJobKind = "normal") -> None:
        """Return capacity previously acquired for ``kind``."""
        weight = self._weight(kind)
        with self._condition:
            if self._acquired[kind] == 0:
                raise RuntimeError(f"cannot release unacquired {kind} capacity")
            self._acquired[kind] -= 1
            self._used -= weight
            self._condition.notify_all()

    @contextmanager
    def slot(
        self,
        kind: HeavyJobKind = "normal",
        *,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[HeavyJobLimiter]:
        """Reserve capacity in the executing thread, reusing an outer reservation."""
        weight = self._weight(kind)
        held = getattr(self._local, "weight", 0)
        if held:
            if weight > held:
                raise RuntimeError("cannot upgrade a held heavy-job reservation")
            if cancel_event is not None and cancel_event.is_set():
                raise HeavyJobCancelledError(f"{kind} job was cancelled")
            yield self
            return
        acquired = self.acquire(kind, timeout=timeout, cancel_event=cancel_event)
        if not acquired:
            if cancel_event is not None and cancel_event.is_set():
                raise HeavyJobCancelledError(f"{kind} job was cancelled while waiting")
            raise HeavyJobLimitTimeoutError(f"timed out waiting for {kind} job capacity")
        try:
            self._local.weight = weight
            yield self
        finally:
            self._local.weight = 0
            self.release(kind)

    def _weight(self, kind: HeavyJobKind) -> int:
        if kind == "exclusive":
            return self.capacity
        try:
            return self._WEIGHTS[kind]
        except KeyError as exc:
            raise ValueError(f"unsupported heavy job kind: {kind!r}") from exc


shared_heavy_job_limiter = HeavyJobLimiter(capacity=2)
# Short alias for entry points that prefer the existing module-singleton naming style.
heavy_job_limiter = shared_heavy_job_limiter
