"""实验运行时进度快照。供寻优/网格轮询 GET 展示当前阶段与 ETA。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def elapsed_ms_since(started_at: str) -> float:
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - started).total_seconds() * 1000)


def format_params(params: dict | None) -> str:
    if not params:
        return ""
    return " · ".join(f"{key}={params[key]}" for key in sorted(params))


def build_runtime(
    *,
    stage: str,
    label: str,
    current: str = "",
    completed: int = 0,
    total: int = 0,
    failed: int = 0,
    ok: int = 0,
    started_at: str,
    last_elapsed_ms: float = 0.0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    elapsed_ms = elapsed_ms_since(started_at)
    remaining = max(0, int(total) - int(completed))
    eta_ms = None
    if completed > 0 and remaining > 0 and elapsed_ms > 0:
        eta_ms = elapsed_ms / completed * remaining
    payload: dict[str, Any] = {
        "stage": stage,
        "label": label,
        "current": current,
        "completed": int(completed),
        "total": int(total),
        "failed": int(failed),
        "ok": int(ok),
        "started_at": started_at,
        "updated_at": utc_now(),
        "elapsed_ms": round(elapsed_ms, 1),
        "eta_ms": None if eta_ms is None else round(eta_ms, 1),
        "last_elapsed_ms": round(float(last_elapsed_ms or 0.0), 1),
    }
    if extra:
        payload.update(extra)
    return payload
