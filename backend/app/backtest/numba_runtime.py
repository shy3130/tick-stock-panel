"""Serialize Numba ``@njit(parallel=True)`` kernels across request threads.

Numba's default ``workqueue`` threading layer is not thread-safe. Concurrent
calls from FastAPI worker threads terminate the process
(``Concurrent access has been detected`` → socket hang up).

A process-wide lock is enough: overlapping kernels queue instead of crashing.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

_NUMBA_PARALLEL_LOCK = threading.RLock()

T = TypeVar("T")


def run_numba_parallel(fn: Callable[[], T]) -> T:
    """Run a Numba parallel kernel under the process-wide lock."""
    with _NUMBA_PARALLEL_LOCK:
        return fn()
