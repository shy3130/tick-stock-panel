"""策略守护 (发布后持续复验) HTTP API。"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.backtest.guards import validate_primary_objective
from app.services import preferences
from app.services.guardian_combinations import create_guardian_composite
from app.services.guardian_schedule import build_guardian_fingerprint, guardian_request
from app.services.guardian_store import (
    GuardianStore,
    GuardianStoreError,
    GuardianStoreValidationError,
    read_combinations,
)
from app.services.mining_jobs import (
    ACTIVE_RUN_STATUSES,
    SUCCESS_RUN_STATUSES,
    MiningRunStoreError,
)
from app.services.mining_manager import MiningJobManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backtest/guardian", tags=["backtest"])


class GuardianConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    guardian_schedule_enabled: bool | None = None
    guardian_watch_pct: float | None = Field(None, gt=0.0, lt=1.0, allow_inf_nan=False)
    guardian_retire_pct: float | None = Field(None, gt=0.0, le=1.0, allow_inf_nan=False)
    guardian_k_max: int | None = Field(None, ge=2, le=4)
    lab_mined_slot_limit: int | None = Field(None, ge=0, le=50)


class GuardianAdoptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    objective: str = "sharpe"


def _manager(request: Request) -> MiningJobManager:
    manager = getattr(request.app.state, "guardian_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="guardian manager is unavailable")
    return manager


def _store(request: Request) -> GuardianStore:
    return GuardianStore(request.app.state.repo.store.data_dir)


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


def _project_last_check(check: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(check, Mapping):
        return None
    return {
        "checked_at": check.get("checked_at"),
        "verdict": check.get("verdict"),
        "skipped": bool(check.get("skipped")),
        "reason": check.get("reason"),
        "error": check.get("error"),
        "degradation_pct": check.get("degradation_pct"),
        "objective": check.get("objective"),
        "window_start": check.get("window_start"),
        "window_end": check.get("window_end"),
        "k": check.get("k"),
        "reasons": check.get("reasons") or [],
    }


def _project_profile(profile: Mapping[str, Any], name: str | None) -> dict[str, Any]:
    baseline = profile.get("baseline") or {}
    metrics = baseline.get("metrics") or {}
    checks = profile.get("checks") or []
    from app.backtest.guardian import consecutive_watch

    streak = consecutive_watch(list(checks))
    return {
        "strategy_id": profile.get("strategy_id"),
        "name": name,
        "status": profile.get("status"),
        "status_reason": profile.get("status_reason"),
        "status_since": profile.get("status_since"),
        "baseline": {
            "kind": baseline.get("kind"),
            "data_as_of": baseline.get("data_as_of"),
            "objective": baseline.get("objective"),
            "score": metrics.get("score"),
            "n_trades": metrics.get("n_trades"),
            "max_drawdown": metrics.get("max_drawdown"),
        },
        "checks_count": len(checks),
        "consecutive_watch": streak,
        "alert": streak >= 2,
        "last_check": _project_last_check(checks[-1] if checks else None),
        "updated_at": profile.get("updated_at"),
    }


@router.get("/overview")
def get_overview(request: Request) -> dict[str, Any]:
    store = _store(request)
    engine = getattr(request.app.state, "strategy_engine", None)
    try:
        profiles = store.list_profiles()
    except GuardianStoreError as exc:
        raise HTTPException(status_code=500, detail="failed to read guardian profiles") from exc
    items = []
    for profile in profiles:
        strategy_id = str(profile.get("strategy_id"))
        name = None
        if engine is not None:
            try:
                name = str(engine.get(strategy_id).meta.get("name") or "") or None
            except ValueError:
                name = None
        items.append(_project_profile(profile, name))
    active = _active_run(_manager(request))
    return {
        "items": items,
        "config": preferences.get_guardian_schedule(),
        "active_run": active,
        "combinations": read_combinations(_data_dir(request)),
    }


@router.post("/{strategy_id}/adopt")
def adopt_strategy(
    strategy_id: str,
    payload: GuardianAdoptRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        validate_primary_objective(payload.objective)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="strategy engine is unavailable")
    try:
        strategy = engine.get(strategy_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"strategy not found: {strategy_id}") from exc
    asset_type = str((strategy.meta.get("asset_types") or ["stock"])[0])
    latest = request.app.state.repo.latest_enriched_date(asset_type)
    if latest is None:
        raise HTTPException(
            status_code=400, detail=f"{asset_type} enriched 数据不可用, 无法建立守护基线"
        )
    manager = _manager(request)
    _reject_active_run(manager)
    store = _store(request)
    try:
        # metrics 留空: trailing_reference 基线由守护运行时首跑标定 (尾随窗口),
        # 语义上不是 OOS, 判定只比较此后新数据上的退化。
        store.create_baseline(
            strategy_id,
            kind="trailing_reference",
            data_as_of=latest.isoformat(),
            objective=payload.objective,
            metrics={},
        )
    except GuardianStoreValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GuardianStoreError as exc:
        raise HTTPException(status_code=500, detail="failed to write guardian profile") from exc
    run = _enqueue_run(manager, request, strategy_ids=[strategy_id])
    return {"ok": True, "strategy_id": strategy_id, "run_id": run.get("run_id")}


@router.post("/{strategy_id}/drop")
def drop_strategy(strategy_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    try:
        removed = store.drop(strategy_id)
    except GuardianStoreValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"guardian profile not found: {strategy_id}")
    return {"ok": True, "strategy_id": strategy_id}


@router.post("/{strategy_id}/rerun")
def rerun_strategy(strategy_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    if store.get(strategy_id) is None:
        raise HTTPException(status_code=404, detail=f"guardian profile not found: {strategy_id}")
    manager = _manager(request)
    _reject_active_run(manager)
    run = _enqueue_run(manager, request, strategy_ids=[strategy_id])
    return {"ok": True, "strategy_id": strategy_id, "run_id": run.get("run_id")}


@router.post("/run")
def run_all(request: Request) -> dict[str, Any]:
    manager = _manager(request)
    _reject_active_run(manager)
    run = _enqueue_run(manager, request, strategy_ids=None)
    return {"ok": True, "run_id": run.get("run_id")}


@router.get("/runs")
def list_runs(request: Request, limit: int = 20) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
    manager = _manager(request)
    try:
        manifests = manager.store.list_runs(limit=limit)
    except (MiningRunStoreError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="failed to read guardian runs") from exc
    return {"items": [_project_run(manager, item) for item in manifests]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    manifest = manager.store.get(run_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"guardian run not found: {run_id}")
    projected = _project_run(manager, manifest)
    if manifest.get("status") in SUCCESS_RUN_STATUSES:
        projected["summary"] = manager.store.read_summary(run_id)
    return projected


@router.post("/combinations/scan")
def scan_combinations(request: Request) -> dict[str, Any]:
    """触发组合相关性扫描 (守护运行时 mode=combinations, 后台执行)。"""
    manager = _manager(request)
    _reject_active_run(manager)
    config = preferences.get_guardian_schedule()
    req = guardian_request(config)
    req["mode"] = "combinations"
    run = manager.start(
        req,
        {
            **build_guardian_fingerprint(request.app.state.repo),
            "source": "manual",
            "mode": "combinations",
        },
        force=True,
        source="manual",
    )
    return {"ok": True, "run_id": run.get("run_id")}


class GuardianComboCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    a: str
    b: str

    @field_validator("a", "b")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value or len(value) > 120:
            raise ValueError("strategy id must contain 1 to 120 characters")
        return value


@router.post("/combinations/create")
def create_combination(
    payload: GuardianComboCreateRequest, request: Request
) -> dict[str, Any]:
    try:
        result = create_guardian_composite(
            request.app.state.repo.store.data_dir,
            request.app.state.strategy_engine,
            left=payload.a,
            right=payload.b,
            latest_enriched=request.app.state.repo.latest_enriched_date("stock"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@router.get("/config")
def get_config() -> dict[str, Any]:
    return preferences.get_guardian_schedule()


@router.patch("/config")
def update_config(payload: GuardianConfigPatch) -> dict[str, Any]:
    current = preferences.get_guardian_schedule()
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="at least one guardian config field is required")
    merged = {**current, **updates}
    try:
        return preferences.set_guardian_schedule(
            merged["guardian_schedule_enabled"],
            merged["guardian_watch_pct"],
            merged["guardian_retire_pct"],
            merged["guardian_k_max"],
            merged["lab_mined_slot_limit"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _active_run(manager: MiningJobManager) -> dict[str, Any] | None:
    manifests = manager.store.list_runs(limit=1, statuses=ACTIVE_RUN_STATUSES)
    if not manifests:
        return None
    return _project_run(manager, manifests[0])


def _reject_active_run(manager: MiningJobManager) -> None:
    if manager.store.list_runs(limit=1, statuses=ACTIVE_RUN_STATUSES):
        raise HTTPException(status_code=409, detail="已有守护复验任务在运行")


def _enqueue_run(
    manager: MiningJobManager,
    request: Request,
    *,
    strategy_ids: list[str] | None,
) -> dict[str, Any]:
    config = preferences.get_guardian_schedule()
    req = guardian_request(config)
    req["strategy_ids"] = strategy_ids
    fingerprint = {
        **build_guardian_fingerprint(request.app.state.repo),
        "source": "manual",
        "strategy_ids": strategy_ids,
    }
    return manager.start(req, fingerprint, force=True, source="manual")


def _project_run(manager: MiningJobManager, manifest: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(manifest["run_id"])
    summary = manager.store.read_summary(run_id)
    return {
        "run_id": run_id,
        "status": manifest.get("status"),
        "request": manifest.get("request") or {},
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"),
        "error": manifest.get("error"),
        "progress": (
            summary.get("progress")
            if isinstance(summary.get("progress"), Mapping)
            else None
        ),
    }
