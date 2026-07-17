"""Spawn-isolated strategy backtest and optimizer task runner."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue
import threading
import time
import traceback
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import psutil


class BacktestWorkerError(RuntimeError):
    """Raised when a spawned worker fails before returning a task result."""


class _PeakRssSampler:
    """Track whole-task and resettable phase RSS peaks with one sampling thread."""

    def __init__(self, interval_seconds: float = 0.05) -> None:
        if interval_seconds <= 0:
            raise ValueError("RSS sample interval must be positive")
        self._process = psutil.Process(os.getpid())
        self._interval_seconds = float(interval_seconds)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._lock = threading.Lock()
        self._started = False
        current = int(self._process.memory_info().rss)
        self.peak_rss_bytes = current
        self._phase_peak_rss_bytes = current

    def start(self) -> None:
        if self._started:
            raise RuntimeError("RSS sampler has already started")
        self._started = True
        self._thread.start()

    def stop(self) -> int:
        if self._started:
            self._stop.set()
            self._thread.join(timeout=1.0)
        self._record_current()
        return self.peak_rss_bytes

    def reset_phase(self) -> None:
        current = int(self._process.memory_info().rss)
        with self._lock:
            self._phase_peak_rss_bytes = current

    def phase_peak_rss_bytes(self) -> int:
        self._record_current()
        with self._lock:
            return self._phase_peak_rss_bytes

    def _record_current(self) -> None:
        current = int(self._process.memory_info().rss)
        with self._lock:
            self.peak_rss_bytes = max(self.peak_rss_bytes, current)
            self._phase_peak_rss_bytes = max(self._phase_peak_rss_bytes, current)

    def _sample(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self._record_current()


def _rss_bytes() -> int:
    return int(psutil.Process(os.getpid()).memory_info().rss)


def _strategy_dirs(data_dir: Path) -> list[Path]:
    app_dir = Path(__file__).resolve().parents[1]
    return [
        app_dir / "strategy" / "builtin",
        data_dir / "strategies" / "custom",
        data_dir / "strategies" / "ai",
    ]


def _decode_backtest_config(payload: dict[str, Any]):
    from app.backtest.strategy import StrategyBacktestConfig

    values = dict(payload)
    values["start"] = date.fromisoformat(values["start"])
    values["end"] = date.fromisoformat(values["end"])
    return StrategyBacktestConfig(**values)


def _decode_optimize_config(payload: dict[str, Any]):
    from app.backtest.optimizer import OptimizeConfig

    values = dict(payload)
    values["start"] = date.fromisoformat(values["start"])
    values["end"] = date.fromisoformat(values["end"])
    return OptimizeConfig(**values)


def _decode_walkforward_config(payload: dict[str, Any]):
    from app.backtest.walkforward import WalkForwardConfig

    values = dict(payload)
    values["start"] = date.fromisoformat(values["start"])
    values["end"] = date.fromisoformat(values["end"])
    return WalkForwardConfig(**values)


def encode_backtest_config(config) -> dict[str, Any]:
    payload = asdict(config)
    payload["start"] = config.start.isoformat()
    payload["end"] = config.end.isoformat()
    return payload


def encode_optimize_config(config) -> dict[str, Any]:
    payload = asdict(config)
    payload["start"] = config.start.isoformat()
    payload["end"] = config.end.isoformat()
    return payload


def make_worker_task(kind: str, data_dir: Path, config) -> dict[str, Any]:
    if kind == "backtest":
        encoded = encode_backtest_config(config)
    elif kind == "optimize":
        encoded = encode_optimize_config(config)
    elif kind == "walkforward":
        encoded = asdict(config)
        encoded["start"] = config.start.isoformat()
        encoded["end"] = config.end.isoformat()
    else:
        raise ValueError(f"unsupported worker task kind: {kind}")
    return {
        "kind": kind,
        "data_dir": str(data_dir.resolve()),
        "config": encoded,
    }


def _attach_worker_metrics(
    kind: str,
    result: dict[str, Any],
    metrics: dict[str, Any],
) -> None:
    if kind == "backtest":
        result.setdefault("stats", {})["worker"] = metrics
    else:
        result["worker"] = metrics


def _worker_entry(task: dict[str, Any], event_queue, cancel_event) -> None:
    sampler = _PeakRssSampler()
    sampler.start()
    started = time.perf_counter()
    store = None
    try:
        from app.backtest.engine import BacktestEngine
        from app.backtest.optimizer import StrategyOptimizer
        from app.backtest.strategy import StrategyBacktestService
        from app.strategy.engine import StrategyEngine
        from app.tickflow.repository import DataStore, KlineRepository

        data_dir = Path(task["data_dir"])
        store = DataStore(data_dir)
        repo = KlineRepository(store)
        strategy_engine = StrategyEngine(strategy_dirs=_strategy_dirs(data_dir))
        service = StrategyBacktestService(BacktestEngine(repo), strategy_engine)

        def _progress(message: dict) -> None:
            event_queue.put({"type": "progress", "payload": message})

        kind = task["kind"]
        if kind == "backtest":
            config = _decode_backtest_config(task["config"])
            result = asdict(service.run(config, _progress, cancel_event))
        elif kind == "optimize":
            config = _decode_optimize_config(task["config"])
            optimizer = StrategyOptimizer(service, strategy_engine)
            result = optimizer.optimize(
                config,
                _progress,
                cancel_event,
                rss_sampler=sampler,
            )
        elif kind == "walkforward":
            from app.backtest.walkforward import WalkForwardService

            config = _decode_walkforward_config(task["config"])
            optimizer = StrategyOptimizer(service, strategy_engine)
            walkforward = WalkForwardService(optimizer, service, strategy_engine)
            result = walkforward.run(config, _progress, cancel_event)
        else:
            raise ValueError(f"unsupported worker task kind: {kind}")

        serialization_started = time.perf_counter()
        serialized_bytes = len(
            json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
        )
        serialization_ms = round(
            (time.perf_counter() - serialization_started) * 1000,
            1,
        )
        peak_rss = sampler.stop()
        metrics = {
            "pid": os.getpid(),
            "peak_rss_bytes": peak_rss,
            "final_rss_bytes": _rss_bytes(),
            "serialization_ms": serialization_ms,
            "serialized_result_bytes": serialized_bytes,
            "task_elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        _attach_worker_metrics(kind, result, metrics)
        event_queue.put({"type": "result", "payload": result})
    except BaseException as exc:
        with suppress(Exception):
            sampler.stop()
        event_queue.put({
            "type": "error",
            "message": str(exc),
            "traceback": traceback.format_exc(),
        })
    finally:
        if store is not None:
            with suppress(Exception):
                store.db.close()


def run_worker_task(
    task: dict[str, Any],
    progress_cb: Callable[[dict], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run one complete task in a spawned process and wait for deterministic exit."""
    context = mp.get_context("spawn")
    events = context.Queue()
    process_cancel = context.Event()
    process = context.Process(
        target=_worker_entry,
        args=(task, events, process_cancel),
        daemon=False,
    )
    parent_rss_before = _rss_bytes()
    try:
        process.start()
    except BaseException:
        events.close()
        events.join_thread()
        raise
    result: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    ipc_started = time.perf_counter()

    try:
        while result is None and failure is None:
            if cancel_event is not None and cancel_event.is_set():
                process_cancel.set()
            try:
                message = events.get(timeout=0.1)
            except queue.Empty:
                if not process.is_alive():
                    break
                continue

            message_type = message.get("type")
            if message_type == "progress":
                if progress_cb is not None:
                    progress_cb(message["payload"])
            elif message_type == "result":
                result = message["payload"]
            elif message_type == "error":
                failure = message

        process.join(timeout=10.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)
            raise BacktestWorkerError("backtest worker returned but did not exit within 10 seconds")
        if failure is not None:
            raise BacktestWorkerError(
                f"{failure.get('message', 'worker failed')}\n{failure.get('traceback', '')}".rstrip()
            )
        if result is None:
            raise BacktestWorkerError(
                f"backtest worker exited without result (exitcode={process.exitcode})"
            )

        parent_metrics = {
            "ipc_elapsed_ms": round((time.perf_counter() - ipc_started) * 1000, 1),
            "parent_rss_before_bytes": parent_rss_before,
            "parent_rss_after_worker_exit_bytes": _rss_bytes(),
            "worker_exitcode": process.exitcode,
        }
        kind = task["kind"]
        if kind == "backtest":
            result.setdefault("stats", {}).setdefault("worker", {}).update(parent_metrics)
        else:
            result.setdefault("worker", {}).update(parent_metrics)
        return result
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)
        events.close()
        events.join_thread()
