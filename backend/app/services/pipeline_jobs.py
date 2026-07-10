"""异步盘后管道任务注册表 — 每个 job 独立 JSON 文件。

设计:
  - job_store/ 文件夹,每个 job 一个 {id}.json,最多保留 max_jobs 个文件
  - running/pending 状态的 job 仅存内存(高频读写)
  - succeeded/failed 后写入独立文件并从内存释放
  - 列表查询 = 内存中的活跃 job + 磁盘文件扫描,按时间排序
  - 单个查询 = 内存优先,没有则读磁盘
  - 创建新 job 前检查文件数量,>= max_jobs 时删除最老的文件
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

JobStatus = Literal["pending", "running", "succeeded", "failed"]

# 运行超过此秒数视为卡死(reload 后孤儿 task / 网络读无限阻塞等)。
# 由 reap_stale() 在 /run 和 /jobs/{id} 轮询端点检查 — 保证卡死后能自愈,
# 无需用户再次点击「同步」。
STALE_JOB_TIMEOUT_S = 600


def _default_store_dir() -> Path:
    from app.config import settings
    return settings.data_dir / "job_store"


_STORE_DIR = _default_store_dir()


class JobStore:
    def __init__(self, max_jobs: int = 50, store_dir: Path = _STORE_DIR) -> None:
        self._max_jobs = max_jobs
        self._store_dir = store_dir
        self._active_jobs: dict[str, dict[str, Any]] = {}   # running/pending
        self._active_id: str | None = None
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

    def create(self) -> tuple[str, bool]:
        """单飞创建任务。返回 (job_id, is_new)。

        去重条件为 **pending ∨ running**(而非仅 running):`/run` 先 create() 再在
        后台任务里 start() 置 running,两者之间存在 pending 窗口。旧实现只在 running 时
        复用,两次快速点击时首个 job 仍是 pending → 第二次绕过去重、另起并发任务、覆盖
        _active_id,导致两条全市场拉取同时读改写同一 parquet。纳入 pending 后该窗口关闭。

        is_new=False 表示复用了已有活跃任务,调用方**不得**再调度新的后台任务。
        """
        with self._lock:
            if self._active_id:
                active = self._active_jobs.get(self._active_id)
                if active and active.get("status") in ("pending", "running"):
                    return self._active_id, False

            job_id = uuid.uuid4().hex[:10]
            self._active_jobs[job_id] = {
                "id": job_id,
                "status": "pending",
                "stage": "init",
                "progress": 0,
                "stage_pct": 0,
                "log": [],
                "started_at": None,
                "finished_at": None,
                "duration_s": None,
                "result": None,
                "error": None,
            }
            self._active_id = job_id
            return job_id, True

    def start(self, job_id: str) -> None:
        with self._lock:
            j = self._active_jobs.get(job_id)
            if not j:
                return
            j["status"] = "running"
            j["started_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    def succeed(self, job_id: str, result: Any) -> None:
        with self._lock:
            j = self._active_jobs.pop(job_id, None)
            if not j:
                return
            j["status"] = "succeeded"
            j["finished_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            j["progress"] = 100
            j["result"] = result
            j["duration_s"] = _duration_s(j)
            if self._active_id == job_id:
                self._active_id = None
            self._delete_oldest()
            self._write_file(j)

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            j = self._active_jobs.pop(job_id, None)
            if not j:
                return
            j["status"] = "failed"
            j["finished_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            j["error"] = error
            j["duration_s"] = _duration_s(j)
            if self._active_id == job_id:
                self._active_id = None
            self._delete_oldest()
            self._write_file(j)

    # ===== progress =====

    def progress(self, job_id: str, stage: str, pct: int, msg: str,
                 stage_pct: int | None = None, skip_log: bool = False) -> None:
        with self._lock:
            j = self._active_jobs.get(job_id)
            if not j:
                return
            j["stage"] = stage
            j["progress"] = max(0, min(100, int(pct)))
            if stage_pct is not None:
                j["stage_pct"] = max(0, min(100, int(stage_pct)))
            elif j["stage"] != stage:
                j["stage_pct"] = 0
            entry = {
                "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "stage": stage,
                "msg": msg,
            }
            if skip_log:
                entry["_skip"] = True
            if skip_log and j["log"] and j["log"][-1].get("stage") == stage and j["log"][-1].get("_skip"):
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

    def reap_stale(self, timeout_s: int = STALE_JOB_TIMEOUT_S) -> None:
        """回收运行超过 timeout_s 的卡死 running job(标记为 failed)。

        在 /run 和 /jobs/{id} 轮询端点都会调用 — 保证卡死后任意轮询都能自愈,
        无需用户再次手动触发同步。reload 后的孤儿 task(内存里已无 job 记录)
        不在此处理:它们没有 active_id,只能靠 executor 线程自然结束或进程重启。
        """
        with self._lock:
            jid = self._active_id
            if not jid:
                return
            j = self._active_jobs.get(jid)
            if not j or j.get("status") != "running":
                return
            started = j.get("started_at")
            if not started:
                return
        # 时间计算放到锁外(避免 datetime 解析持锁)。
        # started_at 形如 "2026-07-04T12:00:00Z"(start() 用 datetime.utcnow 存)。
        # 两端都用 timezone-aware UTC 比较,避免 naive/aware 混用导致 TypeError。
        try:
            start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            elapsed = (datetime.now(start_dt.tzinfo) - start_dt).total_seconds()
        except Exception:  # noqa: BLE001
            return
        if elapsed > timeout_s:
            logger.warning("reap_stale: 强制取消卡死 job %s (已运行 %.0fs)",
                           jid, elapsed)
            self.fail(jid, f"超时自动取消 (运行 {int(elapsed)}s, 疑似卡死)")

    def clear(self) -> None:
        """清空所有任务（内存 + 磁盘文件）。"""
        with self._lock:
            self._active_jobs.clear()
            self._active_id = None
            for f in self._store_dir.glob("*.json"):
                try:
                    f.unlink()
                except Exception:
                    pass


def _summary(j: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": j["id"],
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


# ================================================================
# 重任务互斥锁 — 防「僵尸并发」
# ================================================================
# create() 的单飞去重能挡住 pending/running 窗口内的重复点击, 但挡不住
# reap_stale 把卡死 job 标记 failed、清掉 _active_id 之后 —— 此时 executor
# 线程仍在跑(线程无法被中断), 下一次 /run 会视作无活跃任务而另起一条,
# 与僵尸线程并发读改写同一 parquet。
#
# 该锁绑定「实际执行体(协程/线程)」的生命周期而非 job 状态: 每个重任务在真正
# 开跑前 try_acquire_run_slot(), 结束(含异常)在 finally 里 release_run_slot()。
# 僵尸任务因卡在 executor await 中始终未 release, 新任务 try_acquire 失败 → 快速
# 失败而非并发执行。代价: 真卡死时需重启进程才能再次跑重任务(优先保证数据不损坏)。
_heavy_run_lock = threading.Lock()


def try_acquire_run_slot() -> bool:
    """尝试占用重任务执行槽(非阻塞)。成功返回 True。"""
    return _heavy_run_lock.acquire(blocking=False)


def release_run_slot() -> None:
    """释放重任务执行槽(允许跨线程释放)。"""
    try:
        _heavy_run_lock.release()
    except RuntimeError:
        # 未持有(重复释放)—— 幂等忽略
        pass
