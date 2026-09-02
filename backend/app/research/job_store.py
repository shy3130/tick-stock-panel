"""Durable factor job store.

Each research run maps to ``data/research/factor_jobs/{run_id}.json``.
Run identifiers use a strict whitelist, writes use ``mkstemp`` plus ``fsync``
and ``os.replace``, and terminal states reject stale concurrent updates.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUN_ID_PATTERN = re.compile(r"^rr-[0-9a-f]{16}$")

PENDING = "pending"
RUNNING = "running"
INTERRUPTED = "interrupted"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_JOB_STATUSES = frozenset({COMPLETED, FAILED, CANCELLED, INTERRUPTED})
ACTIVE_JOB_STATUSES = frozenset({PENDING, RUNNING})

MAX_EVENT_PAGE = 200

PATCHABLE_FIELDS = frozenset({"label", "favorite"})

_LOCKS_GUARD = threading.Lock()
_RUN_LOCKS: dict[str, threading.RLock] = {}


def new_run_id() -> str:
    return "rr-" + secrets.token_hex(8)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class InvalidRunIdError(ValueError):
    """Raised when a caller-supplied run_id fails the whitelist."""


class FactorJobStore:
    """File-backed durable job records with an embedded control-event log."""

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / "research" / "factor_jobs"
        self.root.mkdir(parents=True, exist_ok=True)

    # -- helpers -----------------------------------------------------------

    def _path(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise InvalidRunIdError("invalid run_id")
        return self.root / f"{run_id}.json"

    def _thread_lock(self, run_id: str) -> threading.RLock:
        key = str(self._path(run_id))
        with _LOCKS_GUARD:
            return _RUN_LOCKS.setdefault(key, threading.RLock())

    @contextmanager
    def _locked(self, run_id: str) -> Iterator[None]:
        lock_path = self.root / f".{run_id}.lock"
        self._path(run_id)
        with self._thread_lock(run_id):
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                with suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _read_unlocked(self, run_id: str) -> dict[str, Any] | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # -- CRUD --------------------------------------------------------------

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = dict(record)
        stored.setdefault("run_id", new_run_id())
        run_id = stored["run_id"]
        self._path(run_id)
        stored.setdefault("events", [])
        stored.setdefault("job_status", PENDING)
        stored.setdefault("verdict", "inconclusive")
        stored.setdefault("data_status", "missing")
        stored.setdefault("promotion_status", "not_promoted")
        stored.setdefault("favorite", False)
        stored.setdefault("label", None)
        stored.setdefault("source_run_id", None)
        stored.setdefault("preflight", {})
        stored.setdefault("error", None)
        stored.setdefault("finalizing", False)
        created_at = _now()
        stored.setdefault("created_at", created_at)
        stored["updated_at"] = created_at
        with self._locked(run_id):
            _atomic_write_json(self._path(run_id), stored)
        return stored

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._locked(run_id):
            return self._read_unlocked(run_id)

    def patch(self, run_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        """Only label/favorite are mutable; everything else is rejected."""
        unknown = set(fields) - PATCHABLE_FIELDS
        if unknown:
            raise ValueError(f"fields not patchable: {sorted(unknown)}")
        with self._locked(run_id):
            record = self._read_unlocked(run_id)
            if record is None:
                return None
            record.update(fields)
            record["updated_at"] = _now()
            _atomic_write_json(self._path(run_id), record)
            return record

    def transition(self, run_id: str, new_status: str, **fields: Any) -> dict[str, Any] | None:
        """Atomically guard terminal transitions and cancellation during finalization."""
        with self._locked(run_id):
            record = self._read_unlocked(run_id)
            if record is None:
                return None
            current = record.get("job_status")
            if current in TERMINAL_JOB_STATUSES and new_status != current:
                return None
            if new_status == CANCELLED and record.get("finalizing") is True:
                return None
            record["job_status"] = new_status
            record.update(fields)
            if new_status in TERMINAL_JOB_STATUSES:
                record["finalizing"] = False
            record["updated_at"] = _now()
            _atomic_write_json(self._path(run_id), record)
            return record

    def claim_finalization(self, run_id: str) -> dict[str, Any] | None:
        """Atomically reserve artifact publication against concurrent cancellation."""
        with self._locked(run_id):
            record = self._read_unlocked(run_id)
            if (
                record is None
                or record.get("job_status") != RUNNING
                or record.get("finalizing") is True
            ):
                return None
            record["finalizing"] = True
            record["updated_at"] = _now()
            _atomic_write_json(self._path(run_id), record)
            return record

    def update(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        """Compatibility helper; status changes use the terminal guard."""
        if "job_status" in fields:
            status = fields.pop("job_status")
            return self.transition(run_id, status, **fields)
        with self._locked(run_id):
            record = self._read_unlocked(run_id)
            if record is None:
                return None
            record.update(fields)
            record["updated_at"] = _now()
            _atomic_write_json(self._path(run_id), record)
            return record

    # -- events ------------------------------------------------------------

    def append_event(
        self, run_id: str, event_type: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        with self._locked(run_id):
            record = self._read_unlocked(run_id)
            if record is None:
                return None
            events: list[dict[str, Any]] = record.setdefault("events", [])
            event_payload = dict(payload or {})
            event_date = event_payload.pop("date", None) or event_payload.pop(
                "event_date", None
            )
            now = _now()
            envelope = {
                "seq": len(events) + 1,
                "event_type": event_type,
                "run_id": run_id,
                "ts": now,
                "event_date": event_date or now,
                "payload": event_payload,
            }
            events.append(envelope)
            record["updated_at"] = envelope["ts"]
            _atomic_write_json(self._path(run_id), record)
            return envelope

    def events(
        self, run_id: str, cursor: int = 0, limit: int = MAX_EVENT_PAGE
    ) -> list[dict[str, Any]]:
        if limit > MAX_EVENT_PAGE:
            raise ValueError(f"limit must be <= {MAX_EVENT_PAGE}")
        record = self.get(run_id)
        if record is None:
            return []
        return list(record.get("events", []))[cursor : cursor + limit]

    # -- listing / recovery ------------------------------------------------

    def list_runs(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("rr-*.json")):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        return records

    def recover_orphans(self) -> int:
        """Mark every active run interrupted after process restart."""
        recovered = 0
        for record in self.list_runs():
            status = record.get("job_status")
            if status not in ACTIVE_JOB_STATUSES:
                continue
            run_id = record["run_id"]
            error = {
                "code": "worker_recovered",
                "message": f"{status} run interrupted on recovery",
            }
            if self.transition(run_id, INTERRUPTED, error=error) is not None:
                self.append_event(run_id, INTERRUPTED, {"code": "worker_recovered"})
                recovered += 1
        return recovered
