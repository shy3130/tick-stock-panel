"""Persistent run records for the evidence-first research agent."""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .models import china_now_iso, json_safe

_MAX_RUNS = 60
_STALE_SECONDS = 20 * 60
_ACTIVE_STATES = {"queued", "planning", "collecting", "analyzing"}
_MAX_ACTIVE_RUNS = 2


class ResearchRunCapacityError(RuntimeError):
    """Raised when a new expensive research run would exceed the local capacity."""


class ResearchRunStore:
    """Atomic JSON store with a narrow lock around read-modify-write updates."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @staticmethod
    def _path() -> Path:
        from app.config import settings

        path = settings.data_dir / "user_data" / "research_agent_runs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _load_unlocked(self) -> list[dict]:
        path = self._path()
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, list) else []
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    def _save_unlocked(self, records: list[dict]) -> None:
        records = sorted(records, key=lambda item: item.get("created_at", ""), reverse=True)[
            :_MAX_RUNS
        ]
        path = self._path()
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(json_safe(records, max_depth=20), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        with suppress(OSError):
            os.chmod(path, 0o600)

    def create(self, *, symbol: str, name: str, question: str, include_web_news: bool) -> dict:
        now = china_now_iso()
        run = {
            "id": f"rag_{int(time.time() * 1000)}_{os.urandom(3).hex()}",
            "symbol": symbol,
            "name": name,
            "question": question,
            "include_web_news": include_web_news,
            "status": "queued",
            "stage": "等待调度",
            "progress": 0,
            "created_at": now,
            "updated_at": now,
            "started_at": "",
            "completed_at": "",
            "plan": [],
            "evidence": [],
            "answer": "",
            "error": "",
            "runtime": {},
        }
        with self._lock:
            records = self._load_unlocked()
            stale_reaped = self._reap_stale_unlocked(records)
            if self._active_count_unlocked(records) >= _MAX_ACTIVE_RUNS:
                if stale_reaped:
                    self._save_unlocked(records)
                raise ResearchRunCapacityError("研究任务较多,请等待当前任务完成后再试")
            records.append(run)
            self._save_unlocked(records)
        return dict(run)

    def claim(self, run_id: str) -> dict | None:
        """Atomically move one queued run into planning for a single executor."""
        with self._lock:
            records = self._load_unlocked()
            changed = self._reap_stale_unlocked(records)
            for item in records:
                if item.get("id") != run_id:
                    continue
                if item.get("status") != "queued":
                    if changed:
                        self._save_unlocked(records)
                    return None
                now = china_now_iso()
                item.update({
                    "status": "planning",
                    "stage": "规划证据范围",
                    "progress": 5,
                    "started_at": now,
                    "updated_at": now,
                    "error": "",
                })
                self._save_unlocked(records)
                return dict(item)
            if changed:
                self._save_unlocked(records)
        return None

    def get(self, run_id: str) -> dict | None:
        self.reap_stale()
        with self._lock:
            for item in self._load_unlocked():
                if item.get("id") == run_id:
                    return dict(item)
        return None

    def list_recent(self, *, limit: int = 20) -> list[dict]:
        self.reap_stale()
        with self._lock:
            return [dict(item) for item in self._load_unlocked()[:max(1, min(limit, _MAX_RUNS))]]

    def update(self, run_id: str, **patch: Any) -> dict | None:
        with self._lock:
            records = self._load_unlocked()
            for item in records:
                if item.get("id") != run_id:
                    continue
                item.update(json_safe(patch, max_depth=16))
                item["updated_at"] = china_now_iso()
                self._save_unlocked(records)
                return dict(item)
        return None

    def reap_stale(self) -> None:
        """Mark jobs orphaned by a process restart or stuck provider call as failed."""
        with self._lock:
            records = self._load_unlocked()
            if self._reap_stale_unlocked(records):
                self._save_unlocked(records)

    @staticmethod
    def _active_count_unlocked(records: list[dict]) -> int:
        return sum(1 for item in records if item.get("status") in _ACTIVE_STATES)

    @staticmethod
    def _reap_stale_unlocked(records: list[dict]) -> bool:
        cutoff = time.time() - _STALE_SECONDS
        completed_at = ""
        changed = False
        for item in records:
            if item.get("status") not in _ACTIVE_STATES:
                continue
            started = str(item.get("started_at") or item.get("created_at") or "")
            try:
                stamp = datetime_from_iso(started)
            except ValueError:
                stamp = 0.0
            if stamp and stamp >= cutoff:
                continue
            if not completed_at:
                completed_at = china_now_iso()
            item.update({
                "status": "failed",
                "stage": "运行已中断",
                "error": "研究任务在服务重启或超时后未完成,请重新运行",
                "completed_at": completed_at,
                "updated_at": completed_at,
            })
            changed = True
        return changed


def datetime_from_iso(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value).timestamp()


run_store = ResearchRunStore()
