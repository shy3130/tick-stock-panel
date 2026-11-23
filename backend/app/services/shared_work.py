"""Coalesce identical computation/pull units without holding locks during work."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable
from concurrent.futures import Future, TimeoutError
from typing import Any

from app.services.workload import workload


class WorkBusyError(RuntimeError):
    pass


class SharedWork:
    def __init__(
        self, group: str, *, max_pending: int = 64, max_entries: int = 128,
        max_bytes: int = 32 * 1024 * 1024, size_of: Callable = lambda _: 1,
        wait_timeout_s: float = 60,
    ) -> None:
        self.group = group
        self._lock = threading.Lock()
        self._pending: dict[Hashable, Future] = {}
        self._cache: OrderedDict[Hashable, tuple[float, Any, int]] = OrderedDict()
        self._failures: OrderedDict[Hashable, float] = OrderedDict()
        self._bytes = 0
        self._epoch = 0
        self._max_pending, self._max_entries, self._max_bytes = max_pending, max_entries, max_bytes
        self._size_of, self._wait_timeout_s = size_of, wait_timeout_s

    def clear(self) -> None:
        with self._lock:
            self._epoch += 1
            self._cache.clear()
            self._failures.clear()
            self._bytes = 0

    def cache_info(self) -> dict:
        with self._lock:
            return {"entries": len(self._cache), "bytes": self._bytes, "pending": len(self._pending)}

    def _drop(self, key: Hashable) -> None:
        entry = self._cache.pop(key, None)
        if entry is not None:
            self._bytes -= entry[2]

    def run(self, key: Hashable, fn: Callable, *, ttl_s: float = 0) -> Any:
        return self.run_many([key], lambda keys: {key: fn()}, ttl_s=ttl_s)[key]

    def run_many(self, keys: Iterable[Hashable], fn: Callable, *, ttl_s: float = 0) -> dict:
        keys = list(dict.fromkeys(keys))
        ready, futures, owned = {}, {}, []
        now = time.monotonic()
        with self._lock:
            epoch = self._epoch
            for key, entry in list(self._cache.items()):
                if entry[0] <= now:
                    self._drop(key)
            for key, expires in list(self._failures.items()):
                if expires <= now:
                    del self._failures[key]
            for key in keys:
                if ttl_s > 0 and key in self._failures:
                    raise WorkBusyError("上次计算暂时失败，请稍后重试")
                cached = self._cache.get(key) if ttl_s > 0 else None
                if cached is not None:
                    ready[key] = cached[1]
                    self._cache.move_to_end(key)
                    workload.record(self.group, "cache_hit")
                elif key in self._pending:
                    futures[key] = self._pending[key]
                    workload.record(self.group, "shared")
                else:
                    owned.append(key)
            if len(self._pending) + len(owned) > self._max_pending:
                workload.record(self.group, "rejected")
                raise WorkBusyError("计算请求过多，请稍后重试")
            for key in owned:
                self._drop(key)
                self._pending[key] = futures[key] = Future()
        if owned:
            started = time.perf_counter()
            workload.record(self.group, "executed")
            try:
                values = fn(owned)
                # Resolve every owned unit, including explicit empty data, before publishing.
                values = {key: values[key] for key in owned}
                sizes = {key: max(0, int(self._size_of(value))) if ttl_s > 0 else 0
                         for key, value in values.items()}
            except BaseException as exc:
                workload.record(self.group, "failed", elapsed_s=time.perf_counter() - started)
                with self._lock:
                    for key in owned:
                        self._pending.pop(key, None)
                        if ttl_s > 0 and epoch == self._epoch:
                            # Keep only a timestamp, never an exception traceback holding
                            # a large market frame. One failure must not trigger a herd.
                            self._failures[key] = time.monotonic() + 1
                    while len(self._failures) > self._max_entries:
                        self._failures.popitem(last=False)
                for key in owned:
                    futures[key].set_exception(exc)
                raise
            else:
                workload.record(self.group, "completed", elapsed_s=time.perf_counter() - started)
                with self._lock:
                    for key in owned:
                        if ttl_s > 0 and epoch == self._epoch and sizes[key] <= self._max_bytes:
                            self._drop(key)
                            self._cache[key] = (time.monotonic() + ttl_s, values[key], sizes[key])
                            self._bytes += sizes[key]
                        self._pending.pop(key, None)
                    while len(self._cache) > self._max_entries or self._bytes > self._max_bytes:
                        self._drop(next(iter(self._cache)))
                for key in owned:
                    futures[key].set_result(values[key])
        deadline = time.monotonic() + self._wait_timeout_s
        for key, future in futures.items():
            try:
                ready[key] = future.result(timeout=max(0, deadline - time.monotonic()))
            except TimeoutError as exc:
                raise WorkBusyError("共享计算仍在进行，请稍后重试") from exc
        return {key: ready[key] for key in keys}
