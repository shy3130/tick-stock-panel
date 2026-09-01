"""Interactive durable worker with bounded concurrency and cancellation guards."""

from __future__ import annotations

import asyncio
from typing import Any

from .adapters import execute_factor, result_data_status
from .contracts import CancellationToken
from .job_store import CANCELLED, COMPLETED, FAILED, RUNNING, TERMINAL_JOB_STATUSES, FactorJobStore
from .run_store import FactorRunStore

_TASKS: dict[str, asyncio.Task[Any]] = {}
_TOKENS: dict[str, CancellationToken] = {}


class InteractiveWorker:
    MAX_CONCURRENCY = 2

    def __init__(self, jobs: FactorJobStore, runs: FactorRunStore) -> None:
        self.jobs = jobs
        self.runs = runs
        self.semaphore = asyncio.Semaphore(self.MAX_CONCURRENCY)

    def submit(
        self, run_id: str, factor_id: str, repo: Any, scope: Any, parameters: Any
    ) -> asyncio.Task[Any]:
        token = CancellationToken()
        _TOKENS[run_id] = token
        task = asyncio.create_task(self._execute(run_id, factor_id, repo, scope, parameters, token))
        _TASKS[run_id] = task
        task.add_done_callback(lambda _task: (_TASKS.pop(run_id, None), _TOKENS.pop(run_id, None)))
        return task

    async def _execute(
        self,
        run_id: str,
        factor_id: str,
        repo: Any,
        scope: Any,
        parameters: Any,
        token: CancellationToken,
    ) -> None:
        async with self.semaphore:
            if token.cancelled:
                return
            if self.jobs.transition(run_id, "running") is None:
                return
            self.jobs.append_event(run_id, "progress", {"percent": 5, "stage": "running"})
            try:
                result = await asyncio.to_thread(execute_factor, factor_id, repo, scope, parameters)
                current = self.jobs.get(run_id)
                if token.cancelled or (current and current.get("job_status") == CANCELLED):
                    return
                self.runs.publish(
                    run_id,
                    result.model_dump(mode="json"),
                    result.model_dump(mode="json"),
                    [row.model_dump(mode="json") for row in result.events],
                    result.series,
                )
                self.jobs.transition(
                    run_id,
                    COMPLETED,
                    verdict=result.verdict,
                    data_status=result_data_status(
                        result, fallback=str(current.get("data_status") or "ready")
                    ),
                )
                self.jobs.append_event(run_id, "completed", {"verdict": result.verdict})
            except asyncio.CancelledError:
                self.jobs.transition(run_id, CANCELLED)
                self.jobs.append_event(run_id, "cancelled")
            except Exception as exc:
                self.jobs.transition(
                    run_id, FAILED, error={"code": "worker_failed", "message": str(exc)}
                )
                self.jobs.append_event(run_id, "failed", {"message": str(exc)})

    def cancel(self, run_id: str) -> bool:
        current = self.jobs.get(run_id)
        if current is None or current.get("job_status") in TERMINAL_JOB_STATUSES:
            return False
        token = _TOKENS.get(run_id)
        task = _TASKS.get(run_id)
        if token is not None:
            token.cancel()
        if task is not None and current.get("job_status") != RUNNING:
            task.cancel()
        return self.jobs.transition(run_id, CANCELLED) is not None


def recover_orphans(data_dir: Any) -> int:
    return FactorJobStore(data_dir).recover_orphans()
