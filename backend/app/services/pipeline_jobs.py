"""异步盘后管道任务注册表 — 每个 job 独立 JSON 文件。

设计:
  - job_store/ 文件夹,每个 job 一个 {id}.json,最多保留 max_jobs 个文件
  - running/pending 状态的 job 仅存内存(高频读写)
  - succeeded/degraded/failed/cancelled 后写入独立文件并从内存释放
  - 列表查询 = 内存中的活跃 job + 磁盘文件扫描,按时间排序
  - 单个查询 = 内存优先,没有则读磁盘
  - 创建新 job 前检查文件数量,>= max_jobs 时删除最老的文件

可靠性契约 (a6a4bcd 移植):
  - create() 单飞覆盖 pending/running 整个窗口,返回 (job_id, is_new)
  - running job 维护 last_progress_at 心跳;停滞(普通 20min/长任务 30min
    无进度)或总时长超 12h 硬上限时由 reap_stale() 回收为 failed
  - 用户手动取消是独立终态 cancelled(区别于 failed);协作式取消通过
    JobCancelledError(BaseException) 穿透管道内各 stage 的 except Exception
    分块,记录落盘后迟到的 progress() 仍能感知取消
  - 执行槽绑定 owner token:start() 认领、release(owner) 匹配才释放,
    reap 后旧线程的 finish/release 一律失效,不得覆盖新终态
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

JobStatus = Literal["pending", "running", "succeeded", "degraded", "failed", "cancelled"]

# 停滞判定: running 超过该时长没有任何 progress 心跳 → 判死回收
STALL_TIMEOUT_S = 20 * 60  # 普通任务 20 分钟
LONG_RUNNING_STALL_TIMEOUT_S = 30 * 60  # 全市场分钟级长任务 30 分钟
# 总时长硬上限: 持续有心跳也不得超过 12 小时
MAX_TOTAL_RUNTIME_S = 12 * 3600

TERMINAL_STATUSES = ("succeeded", "degraded", "failed", "cancelled")


class JobCancelledError(BaseException):
    """协作式取消信号。

    继承 BaseException 而非 Exception: 管道各 stage 的 `except Exception`
    分块隔离不允许吞掉取消信号, 它必须穿透到 executor worker 的
    `except JobCancelledError` 处理点。
    """


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: str | None) -> datetime | None:
    """解析 Z 后缀 ISO 时间戳为 aware UTC datetime;缺失/畸形返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _default_store_dir() -> Path:
    from app.config import settings

    return settings.data_dir / "job_store"


_STORE_DIR = _default_store_dir()


class JobStore:
    def __init__(self, max_jobs: int = 50, store_dir: Path = _STORE_DIR) -> None:
        self._max_jobs = max_jobs
        self._store_dir = store_dir
        self._active_jobs: dict[str, dict[str, Any]] = {}  # running/pending
        self._active_id: str | None = None
        self._run_owner_job_id: str | None = None
        self._run_owner_token: str | None = None
        self._lock = threading.Lock()
        self._store_dir.mkdir(parents=True, exist_ok=True)

    # ===== persistence =====

    def _write_file(self, job: dict[str, Any]) -> None:
        """将终态 job 写入独立 JSON 文件。"""
        path = self._store_dir / f"{job['id']}.json"
        try:
            path.write_text(
                json.dumps(job, ensure_ascii=False, indent=None),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("failed to write job file %s", path)

    def _read_file(self, job_id: str) -> dict[str, Any] | None:
        """从磁盘读取单个 job 文件。"""
        path = self._store_dir / f"{job_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            logger.warning("failed to read job file %s", path)
            return None

    def _delete_oldest(self) -> None:
        """删除最老的 job 文件,保持文件数量 < max_jobs。"""
        try:
            files = sorted(self._store_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
        except Exception:
            return
        while len(files) >= self._max_jobs:
            oldest = files.pop(0)
            try:
                oldest.unlink()
            except Exception:
                logger.warning("failed to delete old job file %s", oldest)

    def _job_files_sorted(self) -> list[dict[str, Any]]:
        """扫描磁盘上所有 job 文件,按 started_at 从新到旧排序。"""
        jobs: list[dict[str, Any]] = []
        for f in self._store_dir.glob("*.json"):
            try:
                jobs.append(json.loads(f.read_text("utf-8")))
            except Exception:
                continue
        jobs.sort(key=lambda j: j.get("started_at") or "", reverse=True)
        return jobs

    # ===== lifecycle =====

    def create(self, *, kind: str | None = None, long_running: bool = False) -> tuple[str, bool]:
        """创建 job;单飞覆盖 pending/running 整个窗口。

        已有 pending/running 任务时复用该任务,返回 (existing_id, False);
        否则新建,返回 (job_id, True)。long_running 任务用更宽的停滞阈值
        (见 reap_stale)。
        """
        with self._lock:
            if self._active_id:
                existing = self._active_jobs.get(self._active_id)
                if existing and existing["status"] in ("pending", "running"):
                    return self._active_id, False

            job_id = uuid.uuid4().hex[:10]
            self._active_jobs[job_id] = {
                "id": job_id,
                "kind": kind,
                "status": "pending",
                "stage": "init",
                "progress": 0,
                "stage_pct": 0,
                "log": [],
                "created_at": _now_iso(),
                "started_at": None,
                "finished_at": None,
                "last_progress_at": None,
                "long_running": bool(long_running),
                "cancel_requested": False,
                "duration_s": None,
                "result": None,
                "error": None,
            }
            self._active_id = job_id
            return job_id, True

    def start(self, job_id: str) -> str | None:
        """认领唯一执行槽并把 pending job 置为 running。

        返回 owner token；后续 succeed/fail/release 必须携带该 token。
        job 已被取消、重复 start，或执行槽被另一 job 持有时返回 None；
        槽冲突会把当前 pending job 收敛为 failed，避免留下永不启动的记录。
        """
        with self._lock:
            j = self._active_jobs.get(job_id)
            if not j or j["status"] != "pending":
                return None
            if self._run_owner_job_id is not None:
                self._finish_locked(
                    job_id,
                    "failed",
                    error="已有数据任务占用执行槽，请稍后重试",
                )
                return None
            j["status"] = "running"
            j["started_at"] = _now_iso()
            j["last_progress_at"] = j["started_at"]
            owner = uuid.uuid4().hex[:12]
            self._run_owner_job_id = job_id
            self._run_owner_token = owner
            return owner

    def release(self, job_id: str, owner: str) -> None:
        """仅当前 job 的当前 owner 可释放唯一执行槽。"""
        with self._lock:
            if self._run_owner_job_id == job_id and self._run_owner_token == owner:
                self._run_owner_job_id = None
                self._run_owner_token = None

    def cancel(self, job_id: str) -> bool:
        """用户手动取消 → cancelled 终态(区别于 failed)。

        记录立即落盘;执行中的 worker 在下一次 progress() 感知并抛
        JobCancelledError 协作退出。job 不存在或已终态时返回 False。
        """
        with self._lock:
            j = self._active_jobs.get(job_id)
            if not j or j["status"] not in ("pending", "running"):
                return False
            j["cancel_requested"] = True
            self._finish_locked(job_id, "cancelled", error="用户手动取消")
            return True

    def reap_stale(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """回收停滞/超时任务(由 /run 与 job 轮询触发)。

        - 停滞: running 超过阈值无 progress 心跳 → failed
          (普通任务 20 分钟,long_running 30 分钟;持续有心跳绝不因停滞被回收)
        - 硬上限: 总时长超过 12 小时,有心跳也回收 → failed
        - pending 挂起超过停滞阈值(孤儿协程)同样回收 → failed

        返回被回收的 job 列表(空列表 = 无回收)。
        """
        now = now or datetime.now(timezone.utc)
        reaped: list[dict[str, Any]] = []
        with self._lock:
            for job_id in list(self._active_jobs):
                j = self._active_jobs[job_id]
                if j["status"] == "pending":
                    created = _parse_ts(j.get("created_at"))
                    if created and (now - created).total_seconds() > STALL_TIMEOUT_S:
                        reaped.append(
                            self._finish_locked(
                                job_id,
                                "failed",
                                error="任务长时间未启动,自动回收",
                            )
                        )
                    continue
                if j["status"] != "running":
                    continue
                started = _parse_ts(j.get("started_at"))
                if started is None:
                    continue  # 缺时间戳不武断回收
                if (now - started).total_seconds() > MAX_TOTAL_RUNTIME_S:
                    reaped.append(
                        self._finish_locked(
                            job_id,
                            "failed",
                            error="任务总时长超过12小时硬上限,自动回收",
                        )
                    )
                    continue
                last_progress = _parse_ts(j.get("last_progress_at")) or started
                threshold = (
                    LONG_RUNNING_STALL_TIMEOUT_S if j.get("long_running") else STALL_TIMEOUT_S
                )
                if (now - last_progress).total_seconds() > threshold:
                    reaped.append(
                        self._finish_locked(
                            job_id,
                            "failed",
                            error=f"任务超过{threshold // 60}分钟无进度,自动回收",
                        )
                    )
        return reaped

    def _finish_locked(
        self, job_id: str, status: str, *, error: str | None = None, result: Any = None
    ) -> dict[str, Any]:
        """(调用方须持锁)收敛终态: 弹出内存、清理槽位、落盘。返回终态 job。"""
        j = self._active_jobs.pop(job_id, None)
        if not j:
            return {}
        j["status"] = status
        j["finished_at"] = _now_iso()
        if error is not None:
            j["error"] = error
        if result is not None:
            j["result"] = result
            j["progress"] = 100
        j["duration_s"] = _duration_s(j)
        if self._active_id == job_id:
            self._active_id = None
        if self._run_owner_job_id == job_id:
            self._run_owner_job_id = None
            self._run_owner_token = None
        self._delete_oldest()
        self._write_file(j)
        return j

    def succeed(self, job_id: str, result: Any, *, owner: str | None = None) -> None:
        with self._lock:
            if owner is not None and (
                self._run_owner_job_id != job_id or self._run_owner_token != owner
            ):
                logger.debug("stale worker succeed ignored: job=%s", job_id)
                return
            j = self._active_jobs.get(job_id)
            if not j:
                return
            status = (
                "degraded"
                if isinstance(result, dict) and result.get("failed_stages")
                else "succeeded"
            )
            self._finish_locked(job_id, status, result=result)

    def fail(self, job_id: str, error: str, *, owner: str | None = None) -> None:
        with self._lock:
            if owner is not None and (
                self._run_owner_job_id != job_id or self._run_owner_token != owner
            ):
                logger.debug("stale worker fail ignored: job=%s", job_id)
                return
            j = self._active_jobs.get(job_id)
            if not j:
                return
            self._finish_locked(job_id, "failed", error=error)

    # ===== progress =====

    def progress(
        self,
        job_id: str,
        stage: str,
        pct: int,
        msg: str,
        stage_pct: int | None = None,
        skip_log: bool = False,
    ) -> None:
        """记录进度心跳。

        取消已落盘(cancel_requested / 记录已消失)时抛 JobCancelledError,
        由 worker 的 except JobCancelledError 捕获后协作退出 — 这是"记录
        落盘后下一次 progress 仍能感知取消"的路径。终态后的迟到 progress
        (job 已不在内存)同样抛取消,避免旧线程复活终态。
        """
        with self._lock:
            j = self._active_jobs.get(job_id)
            if not j or j.get("cancel_requested") or j["status"] == "cancelled":
                raise JobCancelledError(job_id)
            if j["status"] not in ("pending", "running"):
                raise JobCancelledError(job_id)
            stage_changed = j["stage"] != stage
            j["stage"] = stage
            j["progress"] = max(0, min(100, int(pct)))
            if stage_pct is not None:
                j["stage_pct"] = max(0, min(100, int(stage_pct)))
            elif stage_changed:
                j["stage_pct"] = 0
            j["last_progress_at"] = _now_iso()
            entry = {
                "ts": _now_iso(),
                "stage": stage,
                "msg": msg,
            }
            if skip_log:
                entry["_skip"] = True
            if (
                skip_log
                and j["log"]
                and j["log"][-1].get("stage") == stage
                and j["log"][-1].get("_skip")
            ):
                j["log"][-1] = entry
            else:
                j["log"].append(entry)
                if len(j["log"]) > 200:
                    j["log"] = j["log"][-200:]

    # ===== query =====

    def get(self, job_id: str) -> dict[str, Any] | None:
        # 内存中的活跃 job 优先
        j = self._active_jobs.get(job_id)
        if j:
            return j
        # 否则从磁盘读
        return self._read_file(job_id)

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        # 合并: 内存中的活跃 job + 磁盘文件
        all_jobs: list[dict[str, Any]] = list(self._active_jobs.values())
        all_jobs.extend(self._job_files_sorted())
        # 按 started_at 从新到旧排序,去重(理论上不会有重复)
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for j in sorted(all_jobs, key=lambda x: x.get("started_at") or "", reverse=True):
            jid = j["id"]
            if jid in seen:
                continue
            seen.add(jid)
            result.append(_summary(j))
            if len(result) >= limit:
                break
        return result

    def active_id(self) -> str | None:
        return self._active_id

    def execution_owner(self) -> str | None:
        """返回当前实际执行槽的 job_id，供诊断与契约测试使用。"""
        with self._lock:
            return self._run_owner_job_id

    def clear(self) -> None:
        """清空所有任务（内存 + 磁盘文件）。"""
        with self._lock:
            self._active_jobs.clear()
            self._active_id = None
            self._run_owner_job_id = None
            self._run_owner_token = None
            for f in self._store_dir.glob("*.json"):
                try:
                    f.unlink()
                except Exception:
                    pass


def _summary(j: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": j["id"],
        "kind": j.get("kind"),
        "status": j["status"],
        "stage": j["stage"],
        "progress": j["progress"],
        "stage_pct": j.get("stage_pct", 0),
        "started_at": j["started_at"],
        "finished_at": j["finished_at"],
        "duration_s": j["duration_s"],
        "result": j["result"],
        "error": j["error"],
    }


def _duration_s(j: dict[str, Any]) -> float | None:
    if not j.get("started_at") or not j.get("finished_at"):
        return None
    try:
        s = datetime.fromisoformat(j["started_at"])
        e = datetime.fromisoformat(j["finished_at"])
        return round((e - s).total_seconds(), 2)
    except Exception:  # noqa: BLE001
        return None


# 进程内单例
job_store = JobStore()
