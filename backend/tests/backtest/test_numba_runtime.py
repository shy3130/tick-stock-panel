"""Numba parallel kernels must stay safe under concurrent callers."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from app.backtest.numba_runtime import run_numba_parallel


def test_run_numba_parallel_serializes_concurrent_calls():
    """Two threads must not overlap inside the parallel critical section."""
    active = 0
    max_active = 0
    lock = threading.Lock()

    def work() -> int:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            total = 0
            for i in range(20_000):
                total += i
            return total
        finally:
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: run_numba_parallel(work), range(16)))

    assert max_active == 1


def test_workbench_and_independent_engine_share_the_parallel_lock():
    from tivelo_backtest.research.numba_runtime import run_numba_parallel as engine_run

    holding = threading.Event()
    release = threading.Event()
    started = threading.Event()
    entered = threading.Event()

    def host_work():
        holding.set()
        assert release.wait(5)

    def engine_work():
        started.set()
        engine_run(entered.set)

    with ThreadPoolExecutor(max_workers=2) as pool:
        host = pool.submit(run_numba_parallel, host_work)
        assert holding.wait(5)
        engine = pool.submit(engine_work)
        try:
            assert started.wait(5)
            assert not entered.wait(0.1)
        finally:
            release.set()
        host.result(timeout=5)
        engine.result(timeout=5)
    assert entered.is_set()


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("numba") is None,
    reason="numba not installed on this platform",
)
def test_valid_shift_kernel_survives_concurrent_threads():
    """Regression for workqueue 'Concurrent access has been detected' crashes."""
    from app.backtest.matrix import valid_shift

    rng = np.random.default_rng(0)
    values = rng.normal(size=(64, 32)).astype(np.float32)
    values[::7, ::3] = np.nan

    def once() -> np.ndarray:
        return valid_shift(values, 3)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: once(), range(8)))

    baseline = results[0]
    for other in results[1:]:
        np.testing.assert_allclose(baseline, other, equal_nan=True)
