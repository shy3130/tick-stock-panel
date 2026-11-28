"""Spawned node adapter. Calculation is delegated to the independent engines."""

import os
import threading
import time
from contextlib import suppress

import psutil
from tivelo_contracts.errors import TiveloError

from app.services.mining_process_lock import MiningProcessLock

from .artifacts import execute_node
from .storage import JobError, JobStore


class Observer:
    def __init__(self, store, task, events, cancel):
        self.store, self.task, self.events, self.cancel = store, task, events, cancel
        self._last_check = 0.0
        self._cancelled = False

    def cancelled(self):
        if self.cancel.is_set():
            return True
        current = time.monotonic()
        if current - self._last_check < 0.25:
            return self._cancelled
        state, _ = self.store.current(self.task["job_id"], self.task["node_id"], self.task["token"])
        self._last_check = current
        self._cancelled = state["cancel_requested"]
        return self._cancelled

    def progress(self, done, total, phase):
        if self.cancelled():
            raise JobError("CANCELLED", "研究任务已取消")
        self.events.put(
            {"type": "progress", "payload": {"done": done, "total": total, "phase": phase}}
        )


def _watch_parent(pid, started, cancel, finished):
    while not finished.wait(0.5):
        try:
            alive = psutil.Process(pid).create_time() == started
        except psutil.NoSuchProcess:
            alive = False
        except psutil.AccessDenied:
            # Lack of observation is not evidence that the parent has died.
            continue
        if not alive:
            cancel.set()
            if not finished.wait(5):
                os._exit(75)
            return


def worker_entry(task, events, cancel):
    from app.backtest.worker import _PeakRssSampler

    finished = threading.Event()
    watcher = threading.Thread(
        target=_watch_parent,
        args=(task["owner_pid"], task["owner_started"], cancel, finished),
        name="engine-parent-watch",
        daemon=True,
    )
    watcher.start()
    sampler = _PeakRssSampler(native_data=task.get("native_data", False))
    sampler.start()
    started = time.monotonic()
    lock = None
    try:
        store = JobStore(task["data_dir"])
        plan = store.plan(task["job_id"])
        _, attempt = store.current(task["job_id"], task["node_id"], task["token"])
        directory = store.attempt_directory(task["job_id"], task["node_id"], attempt["number"])
        directory.mkdir(parents=True, exist_ok=True)
        lock = MiningProcessLock(directory)
        lock.acquire(role="engine-node")
        # A new owner can fence a spawned-but-not-started process before it locks.
        store.current(task["job_id"], task["node_id"], task["token"])
        observer = Observer(store, task, events, cancel)
        if observer.cancelled():
            raise JobError("CANCELLED", "研究任务已取消")
        from .models import stages

        role, locked, _ = stages(plan)[task["node_id"]]
        if role == "sync":
            index = int(task["node_id"].rsplit("_", 1)[1])
            source = locked.preparation.sources[index].source
            if source.kind in ("tsp_daily", "tsp_factors", "tsp_minute"):
                from app.data_providers import custom as custom_sources

                from .routed_daily import check_workspace

                check_workspace(store)
                custom_sources.load_all(include_plugins=False)
                if source.kind != "tsp_minute" and source.expected_provider in ("fuyao", "tdxquant"):
                    from app.data_providers.custom.loader import load_bundled_manifest

                    load_bundled_manifest(source.expected_provider)
        result = execute_node(store, plan, task["node_id"], attempt, observer)
        events.put(
            {
                "type": "result",
                "payload": {
                    "artifact": result.model_dump(mode="json"),
                    "worker": {
                        "pid": os.getpid(),
                        "peak_rss_bytes": sampler.stop(),
                        "task_elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    },
                },
            }
        )
    except BaseException as exc:  # noqa: BLE001 — process boundary emits only safe error codes
        code = (
            str(exc.code) if isinstance(exc, (JobError, TiveloError)) else "ENGINE_EXECUTION_FAILED"
        )
        # Arbitrary Python and supplier exception text must not leak through IPC/API.
        events.put({"type": "error", "code": code, "message": "研究节点执行失败", "traceback": ""})
    finally:
        with suppress(Exception):
            sampler.stop()
        if lock is not None:
            lock.release()
        finished.set()
        with suppress(Exception):
            events.close()
            events.join_thread()
        os._exit(0)
