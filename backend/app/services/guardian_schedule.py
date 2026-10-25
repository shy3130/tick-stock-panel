"""每日守护复验调度与确定性 claim。

镜像周度挖掘调度 (mining_schedule) 的纪律: 只做门槛检查与入队, 绝不同步执行
复验; 同一北京交易日只 claim 一次, 重启/重复触发由 run_id 幂等吸收。
挂在 daily_pipeline 成功之后 (数据刚更新, 正是复验的时机)。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.services import preferences
from app.services.guardian_store import GuardianStore
from app.services.mining_schedule import beijing_date

logger = logging.getLogger(__name__)

GUARDIAN_FINGERPRINT_VERSION = "guardian-data-v1"
_CLAIM_LOCK = threading.Lock()


def daily_claim(day: date) -> str:
    return f"guardian-daily-{day.isoformat()}"


def guardian_request(config: dict[str, Any]) -> dict[str, Any]:
    """调度/手动复验共用的 worker 请求体 (阈值来自守护偏好)。"""
    return {
        "strategy_ids": None,
        "thresholds": {
            "watch_pct": config["guardian_watch_pct"],
            "retire_pct": config["guardian_retire_pct"],
            "k_max": config["guardian_k_max"],
        },
    }


def build_guardian_fingerprint(repo: Any) -> dict[str, Any]:
    data_dir = Path(repo.store.data_dir)
    profiles = [
        {"file": path.name, "mtime_ns": path.stat().st_mtime_ns}
        for path in sorted((GuardianStore(data_dir).root).glob("*.json"))
    ]
    digest = hashlib.sha256(
        json.dumps(profiles, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "version": GUARDIAN_FINGERPRINT_VERSION,
        "latest_enriched_date": repo.latest_enriched_date("stock").isoformat()
        if repo.latest_enriched_date("stock") is not None
        else None,
        "profiles": {"count": len(profiles), "digest": digest},
    }


def run_daily_guardian(app_state: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Check the daily gate and enqueue a guardian pass; never run synchronously."""
    config = preferences.get_guardian_schedule()
    if not config["guardian_schedule_enabled"]:
        return {"status": "disabled"}
    day = beijing_date(now)
    if day.weekday() > 4:
        return {"status": "weekday_mismatch"}

    manager = getattr(app_state, "guardian_manager", None)
    repo = getattr(app_state, "repo", None)
    if manager is None or repo is None:
        raise RuntimeError("scheduled guardian dependencies are not initialized")
    store = getattr(manager, "store", None)
    if store is None:
        raise RuntimeError("scheduled guardian manager has no run store")

    claim = daily_claim(day)
    with _CLAIM_LOCK:
        if store.get(claim) is not None:
            return {"status": "already_claimed", "run_id": claim}
        fingerprint = {
            **build_guardian_fingerprint(repo),
            "source": "scheduled",
            "source_claim": claim,
        }
        run = manager.start(
            guardian_request(config),
            fingerprint,
            force=False,
            source="scheduled",
            run_id=claim,
        )
    run_id = run.get("run_id") if isinstance(run, dict) else None
    return {"status": "enqueued", "run_id": run_id or claim}
