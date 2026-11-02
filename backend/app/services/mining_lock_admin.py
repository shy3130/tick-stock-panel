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
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from app.services.mining_jobs import ACTIVE_RUN_STATUSES, MiningRunStoreError
from app.services.mining_process_lock import (
    MiningProcessLock,
    MiningProcessLockError,
    locate_lock_holder_pid,
    probe_lock_held,
    read_lock_holder,
)

logger = logging.getLogger(__name__)

# 挖掘存储静默多久才允许判定持有者陈旧 (秒)。健康实例空闲时也会持锁,
# 该阈值用于排除「正在收尾/写 artifact」的正常窗口。
STALE_AFTER_SECONDS = 600.0
_TERMINATE_GRACE_SECONDS = 6.0
_RELEASE_WAIT_SECONDS = 5.0
_RECOVERY_LOCK = threading.RLock()


class MiningLockAdminError(ValueError):
    """强制释放被拒绝 (fail-closed), message 即拒绝原因。"""


def _finish_takeover(app_state: Any, lock: MiningProcessLock) -> None:
    """Only a new lock owner may recover orphaned runs; never repeat on live runs."""
    try:
        manager = getattr(app_state, "mining_manager", None)
        if manager is not None:
            manager.recover_interrupted()
    except (MiningRunStoreError, OSError) as exc:
        app_state.mining_lock_acquired = False
        lock.release()
        raise MiningLockAdminError("已获取锁，但恢复任务状态失败，请检查数据目录权限后重试") from exc
    app_state.mining_lock_acquired = True


def ensure_mining_lock(app_state: Any) -> bool:
    """Recover a free lock without terminating processes or deleting lock files."""
    with _RECOVERY_LOCK:
        lock = getattr(app_state, "mining_process_lock", None)
        if lock is None:
            # Keep the existing contract for embedded API users without the app lifespan.
            return bool(getattr(app_state, "mining_lock_acquired", True))
        if lock.acquired:
            app_state.mining_lock_acquired = True
            return True
        app_state.mining_lock_acquired = False
        try:
            lock.acquire()
        except MiningProcessLockError:
            return False
        except OSError as exc:
            raise MiningLockAdminError("无法获取挖掘锁，请检查数据目录权限后重试") from exc
        _finish_takeover(app_state, lock)
        return True


def check_and_recover(app_state: Any) -> dict[str, Any]:
    with _RECOVERY_LOCK:
        lock = getattr(app_state, "mining_process_lock", None)
        if lock is None:
            raise MiningLockAdminError("挖掘锁状态不可用")
        ensure_mining_lock(app_state)
        return lock_status(app_state.repo.store.data_dir, lock=lock)


def force_release_for_app(app_state: Any, *, expected_pid: int | None = None) -> dict[str, Any]:
    with _RECOVERY_LOCK:
        lock = getattr(app_state, "mining_process_lock", None)
        if lock is None:
            raise MiningLockAdminError("挖掘锁状态不可用")
        result = force_release(app_state.repo.store.data_dir, lock, expected_pid=expected_pid)
        _finish_takeover(app_state, lock)
        return result


def lock_status(
    data_dir: Path | str,
    *,
    lock: MiningProcessLock | None = None,
    self_pid: int | None = None,
    now: float | None = None,
    stale_after: float = STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """汇总挖掘锁状态: 是否被持有、持有者线索、是否允许强制释放。"""
    pid = os.getpid() if self_pid is None else self_pid
    owns_lock = lock is not None and lock.acquired
    held = owns_lock or probe_lock_held(data_dir)
    holder = read_lock_holder(data_dir)
    holder_source = "file" if holder else None
    if owns_lock and (holder is None or holder.get("pid") != pid):
        # Windows denies reads of the locked byte through a second handle,
        # even in this process. The live owned handle is authoritative.
        holder = {"pid": pid, "role": "app"}
        holder_source = None
    if held and not owns_lock and holder is None:
        # 旧格式/损坏内容 (典型: 旧版本残留后端进程): 从 OS 层面反查持有者,
        # 让这类锁也能走人工确认的强制释放, 而不是让用户手动猜进程。
        located_pid = locate_lock_holder_pid(data_dir)
        if located_pid is not None:
            try:
                created_at = psutil.Process(located_pid).create_time()
            except psutil.Error:
                pass  # Unverifiable identity must not enable process termination.
            else:
                holder = {"pid": located_pid, "role": "unknown", "process_started_at": created_at}
                holder_source = "os-scan"
    mine = owns_lock if lock is not None else (held and bool(holder) and holder.get("pid") == pid)
    holder_pid = holder.get("pid") if holder else None
    holder_alive = bool(mine) or (_pid_alive(holder_pid) if holder_pid else False)
    pid_consistent = (
        _pid_matches_start_time(
            holder_pid, holder.get("started_at"), holder.get("process_started_at"),
        )
        if holder_alive and not mine
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
        "holder_source": holder_source,
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
    expected_pid: int | None = None,
) -> dict[str, Any]:
    """终止陈旧持有者并让本进程接管锁; 任何不满足条件的情况直接拒绝。"""
    status = lock_status(data_dir, lock=lock, stale_after=stale_after)
    if not status["force_releasable"]:
        raise MiningLockAdminError(status["reason"] or "不可强制释放")
    if lock.acquired:
        raise MiningLockAdminError("当前进程已持有挖掘锁")
    target_pid = int(status["holder"]["pid"])
    if expected_pid is not None and expected_pid != target_pid:
        raise MiningLockAdminError("挖掘锁持有者已变化，请重新检查后确认")
    if target_pid == os.getpid():
        raise MiningLockAdminError("不能终止当前后端进程")

    try:
        process = psutil.Process(target_pid)
        created_at = process.create_time()
    except psutil.Error as exc:
        raise MiningLockAdminError("无法核验持有者进程，请重新检查") from exc
    recorded = status["holder"].get("process_started_at")
    if recorded is not None:
        if not isinstance(recorded, (int, float)) or not abs(created_at - recorded) <= 0.01:
            raise MiningLockAdminError("持有者进程已变化，请重新检查后确认")
    elif created_at > datetime.fromisoformat(status["holder"]["started_at"]).timestamp() + 1:
        raise MiningLockAdminError("持有者进程已变化，请重新检查后确认")

    logger.warning(
        "强制释放挖掘锁: 终止持有者 pid=%s (持有于 %s, 存储静默 %ss)",
        target_pid,
        status["holder"].get("started_at"),
        status["store_idle_seconds"],
    )
    _terminate_process(process)
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
        return False, (
            "暂时无法确认占用进程，可能是旧版实例或系统限制读取。"
            "请正常退出其他面板实例，再点击“检查并恢复挖掘”"
        )
    if not holder_alive:
        # 内核会在进程退出时释放 flock, 理论上到不了这里; 防御性兜底
        return False, "持有者进程已不存在, 锁应已释放 — 请刷新状态"
    if not pid_consistent:
        return False, "无法核验持有者身份或 pid 已被复用，请重新检查；不会自动终止进程"
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
    # os.kill(pid, 0) sends a console event on Windows; psutil uses read-only OS queries.
    return psutil.pid_exists(pid)


def _pid_matches_start_time(pid: int, started_at: Any, process_started_at: Any = None) -> bool:
    """Verify process identity on every platform, including after late lock acquisition."""
    try:
        actual = psutil.Process(pid).create_time()
        if process_started_at is not None:
            return isinstance(process_started_at, (int, float)) and abs(actual - process_started_at) <= 0.01
        # Legacy started_at records lock acquisition, which can be long after process start.
        recorded = datetime.fromisoformat(started_at).timestamp()
        return actual <= recorded + 1
    except (psutil.Error, OSError, TypeError, ValueError):
        return False


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


def _terminate_process(process: psutil.Process) -> None:
    try:
        # psutil checks for PID reuse before signalling/terminating this process.
        process.terminate()
    except psutil.NoSuchProcess:
        return
    except psutil.AccessDenied as exc:
        raise MiningLockAdminError(f"无权限终止持有者 pid={process.pid}") from exc


def _wait_condition(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return condition()
