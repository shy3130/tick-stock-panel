"""异步盘后管道任务注册表 — 每个 job 独立 JSON 文件。

设计:
  - job_store/ 文件夹,每个 job 一个 {id}.json,最多保留 max_jobs 个文件
  - running/pending 状态的 job 仅存内存(高频读写)
  - succeeded/degraded/failed 后写入独立文件并从内存释放
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

JobStatus = Literal["pending", "running", "succeeded", "degraded", "failed"]


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

    def create(self, *, kind: str | None = None) -> str:
        with self._lock:
            if self._active_id and self._active_jobs.get(self._active_id, {}).get("status") == "running":
                return self._active_id

            job_id = uuid.uuid4().hex[:10]
            self._active_jobs[job_id] = {
                "id": job_id,
                "kind": kind,
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
            return job_id

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
            j["status"] = (
                "degraded"
                if isinstance(result, dict) and result.get("failed_stages")
                else "succeeded"
            )
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
