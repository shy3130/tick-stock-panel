"""自动策略实验室战报: 守护 + 挖掘 + 名额的聚合只读视图 (P3)。"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.services import preferences
from app.services.guardian_store import read_combinations
from app.services.mining_jobs import MiningRunStoreError

router = APIRouter(prefix="/api/backtest/lab", tags=["backtest"])


def _project_mining_run(store: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    summary = store.read_summary(str(manifest["run_id"]))
    return {
        "run_id": manifest.get("run_id"),
        "status": manifest.get("status"),
        "created_at": manifest.get("created_at"),
        "finished_at": manifest.get("finished_at"),
        "algorithm_version": summary.get("algorithm_version"),
        "confidence": summary.get("confidence"),
        "candidate_count": summary.get("candidate_count"),
        "valid_fold_count": summary.get("valid_fold_count"),
        "refinement": summary.get("refinement"),
        "trials_used": summary.get("trials_used"),
    }


@router.get("/report")
def get_report(request: Request) -> dict[str, Any]:
    data_dir = request.app.state.repo.store.data_dir
    engine = getattr(request.app.state, "strategy_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="strategy engine is unavailable")

    guardian_manager = getattr(request.app.state, "guardian_manager", None)
    guardian: dict[str, Any] = {"profiles": 0, "by_status": {}, "alerts": 0,
                                "last_run": None}
    try:
        from app.services.guardian_store import GuardianStore

        profiles = GuardianStore(data_dir).list_profiles()
    except Exception:  # noqa: BLE001 — 战报对档案读取失败降级为空统计
        profiles = []
    guardian["profiles"] = len(profiles)
    by_status: dict[str, int] = {}
    alerts = 0
    for profile in profiles:
        status = str(profile.get("status") or "pending")
        by_status[status] = by_status.get(status, 0) + 1
        checks = profile.get("checks") or []
        streak = 0
        for check in reversed(checks):
            if check.get("verdict") == "watch" and not check.get("error"):
                streak += 1
            else:
                break
        alerts += 1 if streak >= 2 else 0
    guardian["by_status"] = by_status
    guardian["alerts"] = alerts
    if guardian_manager is not None:
        try:
            runs = guardian_manager.store.list_runs(limit=5)
            guardian["last_run"] = (
                _project_guardian_run(runs[0]) if runs else None
            )
        except MiningRunStoreError:
            guardian["last_run"] = None

    mining_manager = getattr(request.app.state, "mining_manager", None)
    mining: dict[str, Any] = {"last_run": None}
    if mining_manager is not None:
        try:
            runs = mining_manager.store.list_runs(limit=5)
            mining["last_run"] = _project_mining_run(mining_manager.store, runs[0]) if runs else None
        except (MiningRunStoreError, ValueError):
            mining["last_run"] = None

    config = preferences.get_guardian_schedule()
    active_mined = sorted(
        str(meta.get("id"))
        for meta in engine.list_strategies()
        if str(meta.get("id", "")).startswith("mined_factor_")
    )
    combinations = read_combinations(data_dir) or {}
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mining": mining,
        "guardian": guardian,
        "combinations": {
            "generated_at": combinations.get("generated_at"),
            "suggestions": combinations.get("suggestions") or [],
        },
        "slots": {
            "active_mined": len(active_mined),
            "active_mined_ids": active_mined,
            "limit": config["lab_mined_slot_limit"],
            "full": (
                config["lab_mined_slot_limit"] > 0
                and len(active_mined) >= config["lab_mined_slot_limit"]
            ),
        },
    }


def _project_guardian_run(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": manifest.get("run_id"),
        "status": manifest.get("status"),
        "created_at": manifest.get("created_at"),
        "finished_at": manifest.get("finished_at"),
    }
