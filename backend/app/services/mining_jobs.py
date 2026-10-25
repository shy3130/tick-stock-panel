"""Persistent metadata and bounded event storage for mining runs."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import uuid
from collections.abc import Collection, Mapping
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, cast

MiningRunStatus = Literal[
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "succeeded_with_budget_exhausted",
    "failed",
    "cancelled",
    "interrupted",
    "skipped_prerequisite",
]
ArtifactName = Literal["factors", "correlation", "candidates", "folds"]

RUN_STATUSES: frozenset[str] = frozenset(
    {
        "queued",
        "running",
        "cancelling",
        "succeeded",
        "succeeded_with_budget_exhausted",
        "failed",
        "cancelled",
        "interrupted",
        "skipped_prerequisite",
    }
)
ACTIVE_RUN_STATUSES: frozenset[str] = frozenset({"queued", "running", "cancelling"})
SUCCESS_RUN_STATUSES: frozenset[str] = frozenset({"succeeded", "succeeded_with_budget_exhausted"})
TERMINAL_RUN_STATUSES: frozenset[str] = RUN_STATUSES - ACTIVE_RUN_STATUSES
ARTIFACT_NAMES: frozenset[str] = frozenset({"factors", "correlation", "candidates", "folds"})
MAX_EVENTS = 256
MAX_EVENT_PAYLOAD_BYTES = 16 * 1024
_SCHEMA_VERSION = 1
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_STORE_LOCK = threading.RLock()

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset(
        {"running", "cancelling", "cancelled", "failed", "interrupted", "skipped_prerequisite"}
    ),
    "running": frozenset(
        {
            "cancelling",
            "succeeded",
            "succeeded_with_budget_exhausted",
            "failed",
            "cancelled",
            "interrupted",
            "skipped_prerequisite",
        }
    ),
    "cancelling": frozenset(
        {
            "succeeded",
            "succeeded_with_budget_exhausted",
            "failed",
            "cancelled",
            "interrupted",
        }
    ),
    "succeeded": frozenset(),
    "succeeded_with_budget_exhausted": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "interrupted": frozenset(),
    "skipped_prerequisite": frozenset(),
}


class MiningRunStoreError(RuntimeError):
    pass


class MiningRunValidationError(MiningRunStoreError, ValueError):
    pass


class InvalidMiningStatusTransitionError(MiningRunStoreError):
    pass


def canonicalize_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe request whose mapping order cannot affect its signature."""
    if not isinstance(request, Mapping):
        raise MiningRunValidationError("request must be a mapping")
    return cast(dict[str, Any], _canonicalize_json_value(request))


def compute_run_signature(request: Mapping[str, Any], data_fingerprint: Any) -> str:
    """Hash every request dimension and the data fingerprint using BLAKE2b."""
    import hashlib

    signature_input = {
        "request": canonicalize_request(request),
        "data_fingerprint": _canonicalize_json_value(data_fingerprint),
    }
    payload = json.dumps(
        signature_input,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


class MiningRunStore:
    """Store one manifest, summary, artifact registry, and bounded event log per run."""

    def __init__(self, data_dir: Path | str | None = None, *, namespace: str = "mining") -> None:
        if data_dir is None:
            from app.config import settings

            data_dir = settings.data_dir
        if not _NAMESPACE_PATTERN.fullmatch(namespace):
            raise MiningRunValidationError(f"unsupported run store namespace: {namespace!r}")
        self.runs_root = (
            (Path(data_dir).resolve() / "research" / namespace / "runs").resolve()
        )
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        request: Mapping[str, Any],
        data_fingerprint: Any,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a queued run and its initial on-disk files."""
        safe_run_id = self._validate_run_id(uuid.uuid4().hex if run_id is None else run_id)
        canonical_request = canonicalize_request(request)
        canonical_fingerprint = _canonicalize_json_value(data_fingerprint)
        now = _now_iso()
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "run_id": safe_run_id,
            "status": "queued",
            "request": canonical_request,
            "data_fingerprint": canonical_fingerprint,
            "run_signature": compute_run_signature(canonical_request, canonical_fingerprint),
            "artifacts": {},
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "cancellation_requested_at": None,
            "error": None,
        }
        run_dir = self._run_dir(safe_run_id)
        with _STORE_LOCK:
            if run_dir.exists():
                raise MiningRunValidationError(f"run already exists: {safe_run_id}")
            run_dir.mkdir(parents=False)
            _atomic_write_json(run_dir / "summary.json", {})
            _atomic_write_text(run_dir / "events.jsonl", "")
            _atomic_write_json(run_dir / "manifest.json", manifest)
        return manifest

    def get(self, run_id: str) -> dict[str, Any] | None:
        """Read a manifest, filling defaults for manifests written by older versions."""
        safe_run_id = self._validate_run_id(run_id)
        with _STORE_LOCK:
            return self._read_manifest_path(
                self._run_dir(safe_run_id) / "manifest.json", safe_run_id
            )

    def transition_status(
        self,
        run_id: str,
        status: MiningRunStatus,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Apply a validated state transition and atomically replace the manifest."""
        if status not in RUN_STATUSES:
            raise MiningRunValidationError(f"unsupported mining run status: {status!r}")
        safe_run_id = self._validate_run_id(run_id)
        with _STORE_LOCK:
            manifest = self._required_manifest(safe_run_id)
            return self._transition_locked(manifest, status, error=error)

    def write_summary(self, run_id: str, summary: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically replace a run's scalar or compact aggregate summary."""
        if not isinstance(summary, Mapping):
            raise MiningRunValidationError("summary must be a mapping")
        safe_run_id = self._validate_run_id(run_id)
        clean_summary = cast(dict[str, Any], _canonicalize_json_value(summary))
        with _STORE_LOCK:
            self._required_manifest(safe_run_id)
            _atomic_write_json(self._run_dir(safe_run_id) / "summary.json", clean_summary)
        return clean_summary

    def read_summary(self, run_id: str) -> dict[str, Any]:
        safe_run_id = self._validate_run_id(run_id)
        with _STORE_LOCK:
            self._required_manifest(safe_run_id)
            path = self._run_dir(safe_run_id) / "summary.json"
            if not path.exists():
                return {}
            value = _read_json(path)
            if not isinstance(value, dict):
                raise MiningRunStoreError(f"invalid summary for run {safe_run_id}")
            return value

    def artifact_path(self, run_id: str, name: ArtifactName) -> Path:
        """Return the safe default Parquet path for an artifact."""
        safe_run_id = self._validate_run_id(run_id)
        self._validate_artifact_name(name)
        return self._safe_artifact_path(safe_run_id, Path(f"{name}.parquet"))

    def register_artifact(
        self,
        run_id: str,
        name: ArtifactName,
        path: Path | str | None = None,
    ) -> dict[str, Any]:
        """Record a Parquet artifact path relative to its owning run directory."""
        safe_run_id = self._validate_run_id(run_id)
        self._validate_artifact_name(name)
        artifact_path = self._safe_artifact_path(
            safe_run_id,
            Path(path) if path is not None else Path(f"{name}.parquet"),
        )
        if artifact_path.suffix.lower() != ".parquet":
            raise MiningRunValidationError("mining artifacts must use the .parquet suffix")
        run_dir = self._run_dir(safe_run_id)
        relative_path = artifact_path.relative_to(run_dir).as_posix()
        with _STORE_LOCK:
            manifest = self._required_manifest(safe_run_id)
            artifacts = dict(manifest.get("artifacts") or {})
            artifacts[name] = relative_path
            manifest["artifacts"] = artifacts
            manifest["updated_at"] = _now_iso()
            _atomic_write_json(run_dir / "manifest.json", manifest)
        return manifest

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a compact event, retaining only the most recent ``MAX_EVENTS`` entries."""
        safe_run_id = self._validate_run_id(run_id)
        if not isinstance(event_type, str):
            raise MiningRunValidationError("event_type must be a string")
        clean_event_type = event_type.strip()
        if not clean_event_type or len(clean_event_type) > 64:
            raise MiningRunValidationError("event_type must contain 1 to 64 characters")
        raw_payload: Mapping[str, Any] | Any = {} if payload is None else payload
        if not isinstance(raw_payload, Mapping):
            raise MiningRunValidationError("event payload must be a mapping")
        clean_payload = cast(dict[str, Any], _canonicalize_json_value(raw_payload))
        encoded_payload = json.dumps(
            clean_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded_payload) > MAX_EVENT_PAYLOAD_BYTES:
            raise MiningRunValidationError(
                f"event payload exceeds {MAX_EVENT_PAYLOAD_BYTES} byte limit"
            )

        with _STORE_LOCK:
            self._required_manifest(safe_run_id)
            path = self._run_dir(safe_run_id) / "events.jsonl"
            events = self._read_events_path(path)
            next_id = max((event["id"] for event in events), default=0) + 1
            event = {
                "id": next_id,
                "timestamp": _now_iso(),
                "type": clean_event_type,
                "payload": clean_payload,
            }
            events.append(event)
            events = events[-MAX_EVENTS:]
            text = "".join(
                json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
                for item in events
            )
            _atomic_write_text(path, text)
        return event

    def read_events(self, run_id: str, *, after_id: int = 0) -> list[dict[str, Any]]:
        """Read retained events whose monotonically increasing ID is greater than ``after_id``."""
        if isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0:
            raise MiningRunValidationError("after_id must be a non-negative integer")
        safe_run_id = self._validate_run_id(run_id)
        with _STORE_LOCK:
            self._required_manifest(safe_run_id)
            events = self._read_events_path(self._run_dir(safe_run_id) / "events.jsonl")
        return [event for event in events if event["id"] > after_id]

    def list_runs(
        self,
        *,
        limit: int = 50,
        statuses: Collection[MiningRunStatus] | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent valid manifests without exposing store paths to API callers."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise MiningRunValidationError("limit must be between 1 and 200")
        allowed_statuses = None if statuses is None else set(statuses)
        if allowed_statuses is not None and not allowed_statuses <= RUN_STATUSES:
            raise MiningRunValidationError("statuses contains an unsupported mining run status")

        try:
            paths = list(self.runs_root.glob("*/manifest.json"))
        except OSError as exc:
            raise MiningRunStoreError("failed to scan mining run manifests") from exc
        manifests: list[dict[str, Any]] = []
        for path in paths:
            run_id = path.parent.name
            if not _RUN_ID_PATTERN.fullmatch(run_id):
                continue
            try:
                manifest = self._read_manifest_path(
                    self._run_dir(run_id) / "manifest.json",
                    run_id,
                )
            except MiningRunStoreError:
                continue
            if manifest is None:
                continue
            if allowed_statuses is not None and manifest.get("status") not in allowed_statuses:
                continue
            manifests.append(manifest)
        manifests.sort(key=_manifest_sort_key, reverse=True)
        return manifests[:limit]

    def find_by_signature(
        self,
        run_signature: str,
        *,
        statuses: Collection[MiningRunStatus] | None = None,
    ) -> dict[str, Any] | None:
        """Find the newest run with a signature, optionally restricted to selected statuses."""
        if not isinstance(run_signature, str) or not run_signature:
            raise MiningRunValidationError("run_signature must not be empty")
        allowed_statuses = None if statuses is None else set(statuses)
        if allowed_statuses is not None and not allowed_statuses <= RUN_STATUSES:
            raise MiningRunValidationError("statuses contains an unsupported mining run status")

        # Directory enumeration and manifest reads stay outside the write lock. Atomic replacements
        # make each individual read coherent while avoiding a lock around a potentially slow scan.
        try:
            paths = list(self.runs_root.glob("*/manifest.json"))
        except OSError as exc:
            raise MiningRunStoreError("failed to scan mining run manifests") from exc
        matches: list[dict[str, Any]] = []
        for path in paths:
            run_id = path.parent.name
            if not _RUN_ID_PATTERN.fullmatch(run_id):
                continue
            try:
                manifest_path = self._run_dir(run_id) / "manifest.json"
                manifest = self._read_manifest_path(manifest_path, run_id)
            except MiningRunStoreError:
                continue
            if manifest is None or manifest.get("run_signature") != run_signature:
                continue
            if allowed_statuses is not None and manifest.get("status") not in allowed_statuses:
                continue
            matches.append(manifest)
        return max(matches, key=_manifest_sort_key, default=None)

    def recover_interrupted(self) -> int:
        """Mark runs without a live in-process worker as interrupted at startup."""
        try:
            paths = list(self.runs_root.glob("*/manifest.json"))
        except OSError as exc:
            raise MiningRunStoreError("failed to scan mining run manifests") from exc

        candidates: list[str] = []
        for path in paths:
            run_id = path.parent.name
            if not _RUN_ID_PATTERN.fullmatch(run_id):
                continue
            try:
                manifest_path = self._run_dir(run_id) / "manifest.json"
                manifest = self._read_manifest_path(manifest_path, run_id)
            except MiningRunStoreError:
                continue
            if manifest is not None and manifest.get("status") in ACTIVE_RUN_STATUSES:
                candidates.append(run_id)

        recovered = 0
        for run_id in candidates:
            with _STORE_LOCK:
                manifest = self._required_manifest(run_id)
                if manifest["status"] not in ACTIVE_RUN_STATUSES:
                    continue
                self._transition_locked(manifest, "interrupted", error=None)
                recovered += 1
        return recovered

    def _transition_locked(
        self,
        manifest: dict[str, Any],
        status: MiningRunStatus,
        *,
        error: str | None,
    ) -> dict[str, Any]:
        previous = manifest["status"]
        if previous == status:
            return manifest
        if status not in _ALLOWED_TRANSITIONS[previous]:
            raise InvalidMiningStatusTransitionError(
                f"cannot transition from {previous} to {status}"
            )

        now = _now_iso()
        manifest["status"] = status
        manifest["updated_at"] = now
        if status == "running" and not manifest.get("started_at"):
            manifest["started_at"] = now
        if status == "cancelling":
            manifest["cancellation_requested_at"] = now
        if status in TERMINAL_RUN_STATUSES:
            manifest["finished_at"] = now
        if error is not None:
            manifest["error"] = str(error)
        _atomic_write_json(self._run_dir(manifest["run_id"]) / "manifest.json", manifest)
        return manifest

    def _required_manifest(self, run_id: str) -> dict[str, Any]:
        manifest = self._read_manifest_path(self._run_dir(run_id) / "manifest.json", run_id)
        if manifest is None:
            raise KeyError(run_id)
        return manifest

    def _read_manifest_path(self, path: Path, run_id: str) -> dict[str, Any] | None:
        if not path.exists():
            return None
        value = _read_json(path)
        if not isinstance(value, dict):
            raise MiningRunStoreError(f"invalid manifest for run {run_id}")
        return self._normalize_manifest(value, run_id)

    def _normalize_manifest(self, value: dict[str, Any], run_id: str) -> dict[str, Any]:
        status = value.get("status", "queued")
        if status not in RUN_STATUSES:
            raise MiningRunStoreError(f"invalid status in manifest for run {run_id}")
        raw_request = value.get("request") if isinstance(value.get("request"), dict) else {}
        data_fingerprint = value.get("data_fingerprint")
        signature = value.get("run_signature")
        if not isinstance(signature, str) or not signature:
            signature = compute_run_signature(raw_request, data_fingerprint)
        raw_artifacts = value.get("artifacts") if isinstance(value.get("artifacts"), dict) else {}
        artifacts: dict[str, str] = {}
        run_dir = self._run_dir(run_id)
        for name, raw_path in raw_artifacts.items():
            if name not in ARTIFACT_NAMES or not isinstance(raw_path, str):
                continue
            try:
                safe_path = self._safe_artifact_path(run_id, Path(raw_path))
            except MiningRunValidationError:
                continue
            if safe_path.suffix.lower() == ".parquet":
                artifacts[name] = safe_path.relative_to(run_dir).as_posix()
        normalized = dict(value)
        normalized.update(
            {
                "schema_version": value.get("schema_version", 0),
                "run_id": run_id,
                "status": status,
                "request": raw_request,
                "data_fingerprint": data_fingerprint,
                "run_signature": signature,
                "artifacts": artifacts,
                "created_at": value.get("created_at"),
                "updated_at": value.get("updated_at") or value.get("created_at"),
                "started_at": value.get("started_at"),
                "finished_at": value.get("finished_at"),
                "cancellation_requested_at": value.get("cancellation_requested_at"),
                "error": value.get("error"),
            }
        )
        return normalized

    @staticmethod
    def _read_events_path(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise MiningRunStoreError(f"failed to read events file: {path}") from exc
        events: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MiningRunStoreError(f"invalid events file: {path}") from exc
            if (
                not isinstance(event, dict)
                or isinstance(event.get("id"), bool)
                or not isinstance(event.get("id"), int)
                or event["id"] <= 0
            ):
                raise MiningRunStoreError(f"invalid event record: {path}")
            if events and event["id"] <= events[-1]["id"]:
                raise MiningRunStoreError(f"non-monotonic event IDs: {path}")
            events.append(event)
        return events[-MAX_EVENTS:]

    def _run_dir(self, run_id: str) -> Path:
        safe_run_id = self._validate_run_id(run_id)
        candidate = (self.runs_root / safe_run_id).resolve()
        if not candidate.is_relative_to(self.runs_root):
            raise MiningRunValidationError("run path escapes mining runs root")
        return candidate

    def _safe_artifact_path(self, run_id: str, path: Path) -> Path:
        run_dir = self._run_dir(run_id)
        candidate = path if path.is_absolute() else run_dir / path
        resolved = candidate.resolve()
        if not resolved.is_relative_to(run_dir):
            raise MiningRunValidationError("artifact path escapes its mining run directory")
        return resolved

    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        if not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id):
            raise MiningRunValidationError("run_id contains unsafe characters")
        return run_id

    @staticmethod
    def _validate_artifact_name(name: str) -> None:
        if name not in ARTIFACT_NAMES:
            raise MiningRunValidationError(f"unsupported artifact name: {name!r}")


def _canonicalize_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MiningRunValidationError("non-finite numbers are not supported")
        return value
    if isinstance(value, Enum):
        return _canonicalize_json_value(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise MiningRunValidationError("JSON mapping keys must be strings")
            result[key] = _canonicalize_json_value(item)
        return {key: result[key] for key in sorted(result)}
    if isinstance(value, (list, tuple)):
        return [_canonicalize_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_canonicalize_json_value(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            ),
        )
    raise MiningRunValidationError(f"value is not JSON serializable: {type(value).__name__}")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MiningRunStoreError(f"failed to read JSON file: {path}") from exc


def _atomic_write_json(path: Path, value: Any) -> None:
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    except (TypeError, ValueError) as exc:
        raise MiningRunValidationError("value is not JSON serializable") from exc
    _atomic_write_text(path, text)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MiningRunStoreError(f"failed to write file: {path}") from exc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _manifest_sort_key(manifest: dict[str, Any]) -> tuple[str, str]:
    return (
        str(manifest.get("updated_at") or manifest.get("created_at") or ""),
        str(manifest.get("run_id") or ""),
    )
