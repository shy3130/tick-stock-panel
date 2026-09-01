"""Durable factor job store.

Each research run maps to ``data/research/factor_jobs/{run_id}.json``.
Run identifiers use a strict whitelist, writes use ``mkstemp`` plus ``fsync``
and ``os.replace``, and terminal states reject stale concurrent updates.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
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

    # -- CRUD --------------------------------------------------------------

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        stored = dict(record)
        stored.setdefault("run_id", new_run_id())
        self._path(stored["run_id"])  # whitelist validation
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
        created_at = _now()
        stored.setdefault("created_at", created_at)
        stored["updated_at"] = created_at
        _atomic_write_json(self._path(stored["run_id"]), stored)
        return stored

    def get(self, run_id: str) -> dict[str, Any] | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def patch(self, run_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        """Only label/favorite are mutable; everything else is rejected."""
        unknown = set(fields) - PATCHABLE_FIELDS
        if unknown:
            raise ValueError(f"fields not patchable: {sorted(unknown)}")
        record = self.get(run_id)
        if record is None:
            return None
        record.update(fields)
        record["updated_at"] = _now()
        _atomic_write_json(self._path(run_id), record)
        return record

    def transition(self, run_id: str, new_status: str, **fields: Any) -> dict[str, Any] | None:
        """Guarded job_status write: terminal states are final."""
        record = self.get(run_id)
        if record is None:
            return None
        current = record.get("job_status")
        if current in TERMINAL_JOB_STATUSES and new_status != current:
            return None
        record["job_status"] = new_status
        record.update(fields)
        record["updated_at"] = _now()
        _atomic_write_json(self._path(run_id), record)
        return record

    def update(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        """Compatibility helper; status changes use the terminal guard."""
        if "job_status" in fields:
            status = fields.pop("job_status")
            return self.transition(run_id, status, **fields)
        record = self.get(run_id)
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
        record = self.get(run_id)
        if record is None:
            return None
        events: list[dict[str, Any]] = record.setdefault("events", [])
        event_payload = dict(payload or {})
        event_date = event_payload.pop("date", None) or event_payload.pop("event_date", None)
        envelope = {
            "seq": len(events) + 1,
            "event_type": event_type,
            "run_id": run_id,
            "ts": _now(),
            "event_date": event_date or _now(),
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
