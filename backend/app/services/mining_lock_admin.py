"""挖掘锁状态诊断与 fail-closed 强制释放。

背景: 应用进程在 lifespan 抢 `.mining_process.lock`; 当持有者挂死 (典型:
热重载撞上挖掘运行, 旧进程卡在 shutdown 等待收尾) 或 worker 成为孤儿时,
锁会被无限期占用, 后续实例全部无法获得挖掘能力。

设计纪律 (设计稿讨论结论):
- 只提供「人工确认的强制停止按钮」, 不做启动时自动清理 — 终止进程是
  破坏性操作, 必须人来确认。
- 强制释放的判定全部 fail-closed: 持有者不明 (旧格式锁)、pid 可能被
  复用、存在进行中的挖掘运行、挖掘存储近期仍有写入 — 任一命中即拒绝,
  宁可让用户手动处理也不误杀健康实例。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.mining_jobs import ACTIVE_RUN_STATUSES
from app.services.mining_process_lock import (
    MiningProcessLock,
    MiningProcessLockError,
    probe_lock_held,
    read_lock_holder,
)

logger = logging.getLogger(__name__)

# 挖掘存储静默多久才允许判定持有者陈旧 (秒)。健康实例空闲时也会持锁,
# 该阈值用于排除「正在收尾/写 artifact」的正常窗口。
STALE_AFTER_SECONDS = 600.0
_TERMINATE_GRACE_SECONDS = 6.0
_RELEASE_WAIT_SECONDS = 5.0


class MiningLockAdminError(ValueError):
    """强制释放被拒绝 (fail-closed), message 即拒绝原因。"""


def lock_status(
    data_dir: Path | str,
    *,
    self_pid: int | None = None,
    now: float | None = None,
    stale_after: float = STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """汇总挖掘锁状态: 是否被持有、持有者线索、是否允许强制释放。"""
    pid = os.getpid() if self_pid is None else self_pid
    held = probe_lock_held(data_dir)
    holder = read_lock_holder(data_dir)
    mine = held and bool(holder) and holder.get("pid") == pid
    holder_pid = holder.get("pid") if holder else None
    holder_alive = _pid_alive(holder_pid) if holder_pid else False
    pid_consistent = (
        _pid_matches_start_time(holder_pid, holder.get("started_at"))
        if holder_alive
        else True
    )
    active_runs = _count_active_runs(data_dir)
    store_idle = _store_idle_seconds(data_dir, now=time.time() if now is None else now)
    releasable, reason = _classify(
        held=held,
        mine=mine,
        holder_known=holder is not None,
        holder_alive=holder_alive,
        pid_consistent=pid_consistent,
        active_runs=active_runs,
        store_idle=store_idle,
        stale_after=stale_after,
    )
    return {
        "held": held,
        "mine": mine,
        "holder": holder,
        "holder_alive": holder_alive,
        "pid_consistent": pid_consistent,
        "active_runs": active_runs,
        "store_idle_seconds": (
            round(store_idle, 1) if store_idle is not None else None
        ),
        "stale_after_seconds": stale_after,
        "force_releasable": releasable,
        "reason": reason,
    }


def force_release(
    data_dir: Path | str,
    lock: MiningProcessLock,
    *,
    stale_after: float = STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """终止陈旧持有者并让本进程接管锁; 任何不满足条件的情况直接拒绝。"""
    status = lock_status(data_dir, stale_after=stale_after)
    if not status["force_releasable"]:
        raise MiningLockAdminError(status["reason"] or "不可强制释放")
    if lock.acquired:
        raise MiningLockAdminError("当前进程已持有挖掘锁")
    target_pid = int(status["holder"]["pid"])

    logger.warning(
        "强制释放挖掘锁: 终止持有者 pid=%s (持有于 %s, 存储静默 %ss)",
        target_pid,
        status["holder"].get("started_at"),
        status["store_idle_seconds"],
    )
    _terminate_pid(target_pid)
    if not _wait_condition(
        lambda: (not _pid_alive(target_pid)) or (not probe_lock_held(data_dir)),
        _TERMINATE_GRACE_SECONDS + _RELEASE_WAIT_SECONDS,
    ):
        raise MiningLockAdminError(
            f"持有者 pid={target_pid} 未能终止或锁未释放, 请手动处理"
        )
    try:
        lock.acquire()
    except MiningProcessLockError as exc:
        raise MiningLockAdminError(
            "持有者已终止, 但锁仍被其他进程抢先获取; 请重试"
        ) from exc
    return {
        "released": True,
        "terminated_pid": target_pid,
        "reacquired": True,
    }


def _classify(
    *,
    held: bool,
    mine: bool,
    holder_known: bool,
    holder_alive: bool,
    pid_consistent: bool,
    active_runs: int,
    store_idle: float | None,
    stale_after: float,
) -> tuple[bool, str | None]:
    if not held:
        return False, "锁未被持有"
    if mine:
        return False, "锁由当前后端持有"
    if not holder_known:
        return False, "锁文件为旧格式或不可读, 无法定位持有进程 — 请手动查找并结束它"
    if not holder_alive:
        # 内核会在进程退出时释放 flock, 理论上到不了这里; 防御性兜底
        return False, "持有者进程已不存在, 锁应已释放 — 请刷新状态"
    if not pid_consistent:
        return False, "持有者 pid 已被其他进程复用, 拒绝自动终止 — 请手动确认"
    if active_runs:
        return False, f"仍有 {active_runs} 个进行中的挖掘运行, 不能终止持有者"
    if store_idle is not None and store_idle < stale_after:
        return False, (
            f"挖掘存储 {store_idle:.0f} 秒内仍有写入 (陈旧阈值 {stale_after:.0f}s), "
            "持有者可能在正常收尾 — 请稍后再试"
        )
    return True, None


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_matches_start_time(pid: int, started_at: Any) -> bool:
    """POSIX 下比对进程实际启动时间与锁内记录, 排除 pid 复用误杀。

    无法获取 (Windows / ps 不可用 / 记录缺失) 时返回 True —
    该信号只是附加保险, 主判定靠「无活跃 run + 存储静默」。
    """
    if os.name != "posix" or not isinstance(started_at, str):
        return True
    try:
        recorded = datetime.fromisoformat(started_at)
    except ValueError:
        return True
    try:
        output = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        actual = datetime.strptime(output, "%a %b %d %H:%M:%S %Y")
    except (OSError, subprocess.SubprocessError, ValueError):
        return True
    if recorded.tzinfo is not None:
        # ps lstart 是本地时间墙钟; 统一成同口径再比
        recorded = recorded.astimezone().replace(tzinfo=None)
    # ps 的 lstart 无亚秒精度, 放宽到 120s
    return abs((actual - recorded).total_seconds()) <= 120


def _runs_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / "research" / "mining" / "runs"


def _count_active_runs(data_dir: Path | str) -> int:
    count = 0
    for manifest in _runs_dir(data_dir).glob("*/manifest.json"):
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("status") in ACTIVE_RUN_STATUSES:
            count += 1
    return count


def _store_idle_seconds(data_dir: Path | str, *, now: float) -> float | None:
    """挖掘 run 存储最近一次写入距今的秒数; 无任何文件返回 None (视为足够陈旧)。"""
    runs_dir = _runs_dir(data_dir)
    if not runs_dir.is_dir():
        return None
    latest: float | None = None
    for path in runs_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if latest is None or mtime > latest:
            latest = mtime
    if latest is None:
        return None
    return max(0.0, now - latest)


def _terminate_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise MiningLockAdminError(f"无权限终止持有者 pid={pid}") from exc


def _wait_condition(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return condition()
