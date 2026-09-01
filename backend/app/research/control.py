"""Control Plane: single durable factor-run lifecycle.

Every durable factor run is created through this module — the unified API
(`POST /api/research/runs`), schedule run-now, and the APScheduler tick all
share one validation + creation + execution path. No HTTP self-calls and no
legacy run-cards are involved.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import Any

from pydantic import BaseModel

from .catalog import FactorDefinition, get_factor
from .contracts import PreflightRequest, RunScopeModel
from .job_store import ACTIVE_JOB_STATUSES, FactorJobStore, new_run_id
from .pinning import PinnedResearchRepository
from .preflight import preflight
from .run_store import FactorRunStore


class UnknownFactorError(ValueError):
    """Raised when a factor_id is not in the unified registry."""


class PreflightBlocked(RuntimeError):
    """Raised when preflight reports blocking reasons for a run."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.blocking_reasons = [reason.model_dump() for reason in result.blocking_reasons]
        super().__init__("preflight blocked run creation")


def full_market_busy(jobs: FactorJobStore, process: Any = None) -> bool:
    """True when another full-market run is still active."""
    active = any(
        item.get("scope", {}).get("type") == "full_market"
        and item.get("job_status") in ACTIVE_JOB_STATUSES
        for item in jobs.list_runs()
    )
    if active:
        return True
    return process is not None and process.poll() is None


def validate_factor_run_params(params: dict[str, Any]) -> tuple[str, RunScopeModel]:
    """Strict schedule-level validation without a repository.

    Accepts exactly ``{factor_id, scope, parameters}``; the factor must exist in
    the unified registry, and scope/parameters must pass the shared Pydantic
    contracts. Data-side preflight checks run at creation time via
    :func:`plan_factor_run`.
    """
    if not isinstance(params, dict) or set(params) != {"factor_id", "scope", "parameters"}:
        raise ValueError("factor_run params must contain exactly factor_id, scope, parameters")
    factor_id = params["factor_id"]
    definition = get_factor(factor_id) if isinstance(factor_id, str) else None
    if definition is None:
        raise UnknownFactorError(f"unknown factor: {factor_id}")
    scope = RunScopeModel.model_validate(params["scope"])
    if scope.type not in definition.supported_scopes:
        raise ValueError(f"factor {factor_id} does not support scope type {scope.type}")
    definition.request_model.model_validate(params["parameters"])
    return factor_id, scope


def plan_factor_run(
    repo: Any, factor_id: str, scope: Any, parameters: dict[str, Any]
) -> tuple[FactorDefinition, RunScopeModel, Any, BaseModel]:
    """Validate a factor run via the registry, Pydantic contracts, and preflight."""
    definition = get_factor(factor_id) if isinstance(factor_id, str) else None
    if definition is None:
        raise UnknownFactorError(f"unknown factor: {factor_id}")
    scope_model = RunScopeModel.model_validate(scope)
    checked = preflight(
        repo,
        PreflightRequest(factor_id=definition.id, scope=scope_model, parameters=parameters),
    )
    parameters_model = definition.request_model.model_validate(checked.normalized_request)
    if not checked.ready:
        raise PreflightBlocked(checked)
    return definition, scope_model, checked, parameters_model


def create_durable_run(
    jobs: FactorJobStore,
    plan: tuple[FactorDefinition, RunScopeModel, Any, BaseModel],
    *,
    source_run_id: str | None = None,
    origin: dict[str, Any] | None = None,
) -> str:
    """Create the pending durable job record for a validated plan."""
    definition, scope_model, checked, parameters_model = plan
    run_id = new_run_id()
    jobs.create(
        {
            "run_id": run_id,
            "factor_id": definition.id,
            "profile": definition.result_profile,
            "scope": scope_model.model_dump(mode="json"),
            # Persist the complete Pydantic-normalized object.  In particular,
            # full-market workers must not reconstruct or silently drop fields
            # from ``checked.normalized_request``.
            "parameters": parameters_model.model_dump(mode="json"),
            "preflight": checked.model_dump(mode="json"),
            "source_run_id": source_run_id,
            "origin": origin or {"kind": "manual"},
            "job_status": "pending",
            "verdict": "inconclusive",
            "data_status": "ready",
        }
    )
    jobs.append_event(run_id, "snapshot", {"job_status": "pending"})
    return run_id


def run_factor_sync(
    jobs: FactorJobStore,
    runs: FactorRunStore,
    repo: Any,
    run_id: str,
    plan: tuple[FactorDefinition, RunScopeModel, Any, BaseModel],
) -> dict[str, Any]:
    """Execute a pending durable run to a terminal state in the caller's thread.

    Used by the scheduler (no event loop); the interactive API path uses
    ``InteractiveWorker.submit`` for the same lifecycle. Job/run records are
    never rewritten afterwards — runs are immutable facts.
    """
    from .adapters import execute_factor, result_data_status

    definition, scope_model, _checked, parameters_model = plan
    running = jobs.transition(run_id, "running")
    if running is None:
        return jobs.get(run_id) or {}
    jobs.append_event(run_id, "progress", {"percent": 5, "stage": "running"})
    try:
        pinned_repo = PinnedResearchRepository.bind(repo, running, definition)
        result = execute_factor(definition.id, pinned_repo, scope_model, parameters_model)
        claimed = jobs.claim_finalization(run_id)
        if claimed is None:
            return jobs.get(run_id) or {}
        runs.publish(
            run_id,
            result.model_dump(mode="json"),
            result.model_dump(mode="json"),
            [row.model_dump(mode="json") for row in result.events],
            result.series,
        )
        completed = jobs.transition(
            run_id,
            "completed",
            verdict=result.verdict,
            data_status=result_data_status(
                result, fallback=str(claimed.get("data_status") or "ready")
            ),
        )
        if completed is not None:
            jobs.append_event(run_id, "completed", {"verdict": result.verdict})
    except Exception as exc:
        failed = jobs.transition(
            run_id, "failed", error={"code": "worker_failed", "message": str(exc)}
        )
        if failed is not None:
            jobs.append_event(run_id, "failed", {"message": str(exc)})
    return jobs.get(run_id) or {}


def spawn_full_market_worker(data_dir: str, run_id: str) -> subprocess.Popen:
    """Spawn the controlled full-market worker subprocess for a durable run."""
    worker_env = dict(os.environ)
    worker_env["DATA_DIR"] = data_dir
    return subprocess.Popen(
        [sys.executable, "-m", "app.research.worker", "--run-id", run_id],
        env=worker_env,
    )


def watch_full_market_process(
    process: subprocess.Popen, jobs: FactorJobStore, run_id: str
) -> threading.Thread:
    """Reconcile an asynchronously spawned worker's hard or abnormal exit."""

    def watch() -> None:
        try:
            return_code = process.wait()
        except OSError as exc:
            return_code = -1
            message = str(exc)
        else:
            message = f"worker exited with code {return_code}"
        if return_code == 0:
            return
        current = jobs.get(run_id)
        if current is None or current.get("job_status") in {
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        }:
            return
        code = "runner_rss_exceeded" if return_code == 75 else "runner_exit"
        failed = jobs.transition(run_id, "failed", error={"code": code, "message": message})
        if failed is not None:
            jobs.append_event(run_id, "failed", {"code": code, "message": message})

    thread = threading.Thread(
        target=watch,
        name=f"research-run-watch-{run_id}",
        daemon=True,
    )
    thread.start()
    return thread
