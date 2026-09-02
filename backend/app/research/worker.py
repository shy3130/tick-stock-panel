"""Controlled full-market worker; Web may pass only a validated run identifier."""

from __future__ import annotations

import argparse
import fcntl
import os
import resource
import sys
import tempfile
import threading
from contextlib import contextmanager, suppress
from datetime import date
from pathlib import Path
from typing import Any

from app.research.adapters import _norm, result_data_status
from app.research.catalog import get_factor
from app.research.job_store import (
    CANCELLED,
    COMPLETED,
    FAILED,
    RUNNING,
    FactorJobStore,
    InvalidRunIdError,
)
from app.research.pinning import PinnedResearchRepository
from app.research.run_store import FactorRunStore
from app.services.full_market_research import FullMarketRunnerError, run_full_market_research
from app.storage.repository import DataStore, KlineRepository

DEFAULT_MAX_RSS_GIB = 8.0
DEFAULT_LOCK_PATH = Path(tempfile.gettempdir()) / "tickflow-full-market-research.lock"
THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "POLARS_MAX_THREADS",
)
for _name in THREAD_ENV_VARS:
    try:
        _value = int(os.environ.get(_name, "2"))
    except ValueError:
        _value = 2
    os.environ[_name] = str(min(max(_value, 1), 2))


@contextmanager
def single_run_lock(path: Path = DEFAULT_LOCK_PATH):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FullMarketRunnerError("full_market_busy") from exc
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


@contextmanager
def rss_guard(max_rss_gib: float = DEFAULT_MAX_RSS_GIB):
    if not 0 < max_rss_gib <= 64:
        raise ValueError("max-rss-gib must be within (0, 64]")
    limit = int(max_rss_gib * 1024**3)
    stop = threading.Event()

    def monitor() -> None:
        while not stop.wait(0.25):
            if _peak_rss_bytes() > limit:
                os._exit(75)

    watcher = threading.Thread(target=monitor, name="full-market-rss-guard", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stop.set()
        watcher.join(timeout=2.0)


def _build_runtime() -> tuple[Path, KlineRepository]:
    store = DataStore()
    return store.data_dir, KlineRepository(store)


def execute_job(
    record: dict[str, Any], repo: Any, jobs: FactorJobStore, runs: FactorRunStore
) -> int:
    run_id = record["run_id"]
    if record.get("scope", {}).get("type") != "full_market":
        raise FullMarketRunnerError("worker_scope_not_full_market")
    factor = get_factor(record.get("factor_id", ""))
    if factor is None or factor.full_market_executor is None:
        raise FullMarketRunnerError("full_market_executor_unavailable")
    running = jobs.transition(run_id, RUNNING)
    if running is None:
        return 0
    params = factor.request_model.model_validate(record.get("parameters") or {})
    start = getattr(params, "start", None)
    end = getattr(params, "end", None)
    if not isinstance(start, date) or not isinstance(end, date):
        raise FullMarketRunnerError("full_market_date_window_missing")
    try:
        pinned_repo = PinnedResearchRepository.bind(repo, running, factor)
        payload = run_full_market_research(
            factor.id,
            pinned_repo,
            start,
            end,
            oos_start=getattr(params, "oos_start", None),
            cost_bps=getattr(params, "cost_bps", None),
            parameters=params.model_dump(mode="python"),
        )
        current = jobs.get(run_id)
        if current is None or current.get("job_status") == CANCELLED:
            return 0
        claimed = jobs.claim_finalization(run_id)
        if claimed is None:
            return 0
        normalized = _norm(factor.result_profile, payload)
        summary = normalized.model_dump(mode="json")
        runs.publish(
            run_id,
            summary,
            payload,
            [row.model_dump(mode="json") for row in normalized.events],
            normalized.series,
        )
        completed = jobs.transition(
            run_id,
            COMPLETED,
            verdict=normalized.verdict,
            data_status=result_data_status(
                normalized, fallback=str(claimed.get("data_status") or "ready")
            ),
        )
        if completed is not None:
            jobs.append_event(run_id, "completed", {"verdict": normalized.verdict})
        return 0
    except Exception as exc:
        failed = jobs.transition(
            run_id, FAILED, error={"code": "worker_failed", "message": str(exc)}
        )
        if failed is not None:
            jobs.append_event(run_id, "failed", {"message": str(exc)})
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    data_dir, repo = _build_runtime()
    jobs = FactorJobStore(data_dir)
    try:
        record = jobs.get(args.run_id)
    except InvalidRunIdError:
        return 2
    if record is None:
        return 2
    runs = FactorRunStore(data_dir)
    try:
        with single_run_lock(), rss_guard():
            with suppress(OSError):
                os.nice(10)
            return execute_job(record, repo, jobs, runs)
    except FullMarketRunnerError as exc:
        code = "lock_conflict" if str(exc) == "full_market_busy" else "worker_failed"
        failed = jobs.transition(
            args.run_id,
            FAILED,
            error={"code": code, "message": str(exc)},
        )
        if failed is not None:
            jobs.append_event(args.run_id, "failed", {"code": code, "message": str(exc)})
        return 1
    except Exception as exc:
        failed = jobs.transition(
            args.run_id, FAILED, error={"code": "worker_failed", "message": str(exc)}
        )
        if failed is not None:
            jobs.append_event(args.run_id, "failed", {"message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
