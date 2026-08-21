"""服务重启后的回测任务恢复扫描。

规则 (契约 V1 模块 A):
1. backtest_jobs/*.json: DurableJob status in {queued, running} 且
   lease_owner != LEASE_OWNER (历史进程遗留) → interrupted
2. optimizer_experiments/*.json: status in {pending, running} → interrupted
3. parameter_grid_experiments/*.json: status in {pending, running} → interrupted
4. TTL 清理: completed/cancelled/failed 且 updated_at 超 24h 删除;
   interrupted 超 7 天删除

要点:
- cancelled 属终态, 永不参与 interrupted 改写
- optimizer/grid 的改写在原始 JSON dict 上进行 (不 round-trip dataclass),
  避免丢掉旧版本/新版本携带的未知字段 (如 request 快照)
- 单文件损坏 (非法 JSON / schema 不合) 只跳过计数, 不中断整体扫描
- 不自动开跑任何任务 (重启不得顺手打满 CPU)
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.backtest.job_store import (
    LEASE_OWNER,
    BacktestJobStore,
    JobIdError,
    check_job_id,
    now_iso,
)

# 终态记录保留 24h, interrupted 可 resume 但超 7 天视为放弃。
TERMINAL_TTL = timedelta(hours=24)
INTERRUPTED_TTL = timedelta(days=7)

_JOB_STALE_STATUSES = frozenset({"queued", "running"})
_EXPERIMENT_STALE_STATUSES = frozenset({"pending", "running"})
_TERMINAL_JOB_STATUSES = frozenset({"completed", "cancelled", "failed"})


def _parse_iso(value: Any) -> datetime | None:
    """解析 updated_at; 失败返回 None (调用方保守处理: 不删除)。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _is_expired(status: str, updated_at: Any, now: datetime) -> bool:
    stamp = _parse_iso(updated_at)
    if stamp is None:
        return False
    ttl = INTERRUPTED_TTL if status == "interrupted" else TERMINAL_TTL
    return now - stamp > ttl


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """与各实验 store 同口径的原子覆盖: tmp + os.replace。"""
    data = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, default=str)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def _recover_experiment_dir(dir_path: Path) -> tuple[int, int]:
    """把实验目录中 pending/running 的 JSON 标为 interrupted。

    返回 (interrupted, skipped_corrupt)。
    """
    interrupted = 0
    skipped = 0
    if not dir_path.exists():
        return 0, 0
    for path in sorted(dir_path.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped += 1
            continue
        if not isinstance(payload, dict):
            skipped += 1
            continue
        # cancelled/completed/failed 等终态原样保留。
        if payload.get("status") not in _EXPERIMENT_STALE_STATUSES:
            continue
        payload["status"] = "interrupted"
        payload["updated_at"] = now_iso()
        try:
            _atomic_write_json(path, payload)
        except OSError:
            skipped += 1
            continue
        interrupted += 1
    return interrupted, skipped


def _recover_job_dir(store: BacktestJobStore, *, now: datetime) -> tuple[int, int, int]:
    """backtest_jobs 扫描: 外来 lease 的活跃任务标 interrupted + TTL 清理。

    返回 (interrupted, deleted, skipped_corrupt)。
    """
    interrupted = 0
    deleted = 0
    skipped = 0
    job_dir = store.job_dir
    if not job_dir.exists():
        return 0, 0, 0
    for path in sorted(job_dir.glob("*.json")):
        try:
            check_job_id(path.stem)
        except JobIdError:
            skipped += 1
            continue
        job = store.get(path.stem)
        if job is None:
            skipped += 1
            continue
        if job.status in _JOB_STALE_STATUSES:
            if job.lease_owner == LEASE_OWNER:
                # 本进程刚写入的活跃任务 (正常并发), 不动。
                continue
            job.status = "interrupted"
            job.updated_at = now_iso()
            store.save(job)
            interrupted += 1
            # 落盘后继续按新 status 评估 TTL (updated_at 已刷新, 不会立即过期)。
        if job.status in _TERMINAL_JOB_STATUSES or job.status == "interrupted":
            if _is_expired(job.status, job.updated_at, now):
                if store.delete(job.job_id):
                    deleted += 1
    return interrupted, deleted, skipped


def recover_stale_backtest_jobs(data_dir: Path | str) -> dict[str, int]:
    """启动时恢复磁盘遗留任务; 只标记/清理, 不自动开跑。"""
    store = BacktestJobStore(data_dir)
    now = datetime.now(UTC)
    jobs_interrupted, jobs_deleted, jobs_skipped = _recover_job_dir(store, now=now)
    opt_interrupted, opt_skipped = _recover_experiment_dir(
        store.job_dir.parent / "optimizer_experiments"
    )
    grid_interrupted, grid_skipped = _recover_experiment_dir(
        store.job_dir.parent / "parameter_grid_experiments"
    )
    return {
        "jobs_interrupted": jobs_interrupted,
        "optimizer_interrupted": opt_interrupted,
        "grid_interrupted": grid_interrupted,
        "jobs_deleted": jobs_deleted,
        "skipped_corrupt": jobs_skipped + opt_skipped + grid_skipped,
    }
