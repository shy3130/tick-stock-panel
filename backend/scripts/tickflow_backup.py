"""Create and verify offline TickFlow data snapshots on the Windows host.

The production container is stopped before copying the bind-mounted data and is
restored to its original running state afterwards. Snapshots intentionally omit
credentials, authentication state and transient files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

SCHEMA_VERSION = 1
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BACKUP_ROOT = Path(r"D:\A股-v2-backups")
CONTAINER_NAME = "TickFlow_Stock_Panel"
_SNAPSHOT_RE = re.compile(
    r"^tickflow-(?P<date>\d{8})-(?P<time>\d{6})(?:-[0-9a-f]{8})?\.complete$"
)
_SUSPICIOUS_NAME_RE = re.compile(
    r"(?:^|[._-])(auth|credential|password|secret|token|api[._-]?key|private[._-]?key)(?:[._-]|$)",
    re.IGNORECASE,
)
_TRANSIENT_SUFFIXES = (".lock", ".tmp", ".partial")
_PRIVATE_KEY_SUFFIXES = (".key", ".pem", ".p12", ".pfx")
_TEXT_CONFIG_SUFFIXES = {
    ".cfg",
    ".conf",
    ".ini",
    ".js",
    ".json",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r'''(?im)["']?(password|passwd|secret|token|api[_-]?key|authorization|private[_-]?key)'''
    r'''["']?\s*[:=]\s*["']?([^\s"',}\]]+)'''
)


class SnapshotValidationError(RuntimeError):
    """Raised when a snapshot cannot prove its integrity."""


@dataclass(frozen=True)
class ContainerState:
    exists: bool
    running: bool
    healthy: bool
    container_id: str = ""
    image: str = ""


@dataclass(frozen=True)
class ManifestEntry:
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ValidationReport:
    path: Path
    file_count: int
    total_bytes: int


class ContainerLifecycle(Protocol):
    def inspect(self) -> ContainerState: ...

    def stop(self) -> None: ...

    def start_and_wait_healthy(self) -> None: ...


class DockerComposeLifecycle:
    """Lifecycle adapter for the single Compose app service."""

    def __init__(self, project_root: Path, timeout_s: int = 150) -> None:
        self.project_root = project_root
        self.timeout_s = timeout_s

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=self.project_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=check,
        )

    def inspect(self) -> ContainerState:
        result = self._run(
            [
                "docker",
                "inspect",
                CONTAINER_NAME,
                "--format",
                "{{json .}}",
            ],
            check=False,
        )
        if result.returncode != 0:
            return ContainerState(exists=False, running=False, healthy=False)
        payload = json.loads(result.stdout)
        state = payload.get("State") or {}
        health = (state.get("Health") or {}).get("Status")
        image = (payload.get("Config") or {}).get("Image") or ""
        return ContainerState(
            exists=True,
            running=bool(state.get("Running")),
            healthy=health == "healthy",
            container_id=str(payload.get("Id") or "")[:12],
            image=image,
        )

    def stop(self) -> None:
        self._run(["docker", "compose", "stop", "app"])
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if not self.inspect().running:
                return
            time.sleep(0.5)
        raise TimeoutError("TickFlow container did not stop before backup")

    def start_and_wait_healthy(self) -> None:
        self._run(["docker", "compose", "start", "app"])
        deadline = time.monotonic() + self.timeout_s
        last = ContainerState(exists=False, running=False, healthy=False)
        while time.monotonic() < deadline:
            last = self.inspect()
            if last.running and last.healthy:
                return
            time.sleep(1.0)
        raise TimeoutError(
            "TickFlow container did not return healthy after backup "
            f"(running={last.running}, healthy={last.healthy})"
        )


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _validate_roots(project_root: Path, backup_root: Path) -> tuple[Path, Path, Path]:
    project_root = _resolved(project_root)
    backup_root = _resolved(backup_root)
    data_dir = _resolved(project_root / "data")
    if not (project_root / "docker-compose.yml").is_file():
        raise ValueError(f"unexpected project root: {project_root}")
    if not data_dir.is_dir():
        raise ValueError(f"production data directory is missing: {data_dir}")
    if backup_root == data_dir or data_dir in backup_root.parents:
        raise ValueError("backup root must not be inside the production data directory")
    if backup_root == project_root or project_root in backup_root.parents:
        raise ValueError("backup root must not be inside the project directory")
    return project_root, backup_root, data_dir


def exclusion_reason(relative_path: Path) -> str | None:
    """Return why a data-relative path must never enter a snapshot."""
    name = relative_path.name.lower()
    if name == ".env" or name.startswith(".env."):
        return "environment file"
    if name.endswith(_TRANSIENT_SUFFIXES):
        return "transient file"
    if name.endswith(_PRIVATE_KEY_SUFFIXES):
        return "private key or certificate"
    if name == "auth.json" or name.startswith("auth.json."):
        return "authentication state"
    if name == "secrets.json" or name.startswith("secrets.json."):
        return "secret store"
    if _SUSPICIOUS_NAME_RE.search(name):
        return "suspicious credential-like filename"
    return None


def _content_exclusion_reason(source: Path) -> str | None:
    """Fail closed when a small text/config file contains a credential assignment."""
    if source.suffix.lower() not in _TEXT_CONFIG_SUFFIXES or source.stat().st_size > 4 * 1024 * 1024:
        return None
    try:
        content = source.read_text("utf-8", errors="ignore")
    except OSError:
        return "unreadable text/config file"
    if _SENSITIVE_ASSIGNMENT_RE.search(content):
        return "credential-like content"
    return None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_optional(path: Path) -> str | None:
    return _hash_file(path) if path.is_file() else None


def _iter_source_files(data_dir: Path) -> Iterable[tuple[Path, Path, str | None]]:
    for root, dir_names, file_names in os.walk(data_dir, followlinks=False):
        root_path = Path(root)
        for directory in list(dir_names):
            candidate = root_path / directory
            if candidate.is_symlink():
                raise ValueError(f"symbolic links are not allowed in data: {candidate}")
        for file_name in sorted(file_names):
            source = root_path / file_name
            if source.is_symlink():
                raise ValueError(f"symbolic links are not allowed in data: {source}")
            relative = source.relative_to(data_dir)
            if any(char in relative.as_posix() for char in ("\n", "\r", "\t")):
                raise ValueError(f"unsupported control character in path: {relative}")
            reason = exclusion_reason(relative)
            if reason is None:
                reason = _content_exclusion_reason(source)
            yield source, relative, reason


def _copy_source_files(
    data_dir: Path, staging: Path
) -> tuple[list[ManifestEntry], list[str]]:
    entries: list[ManifestEntry] = []
    excluded: list[str] = []
    payload_root = staging / "data"
    payload_root.mkdir(parents=True)
    for source, relative, reason in _iter_source_files(data_dir):
        relative_text = relative.as_posix()
        if reason:
            excluded.append(relative_text)
            continue
        destination = payload_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        entries.append(
            ManifestEntry(
                relative_path=f"data/{relative_text}",
                size=destination.stat().st_size,
                sha256=_hash_file(destination),
            )
        )
    entries.sort(key=lambda entry: entry.relative_path)
    excluded.sort()
    return entries, excluded


def _write_manifest(path: Path, entries: list[ManifestEntry]) -> None:
    lines = [
        f"{entry.sha256}\t{entry.size}\t{entry.relative_path}\n" for entry in entries
    ]
    path.write_text("".join(lines), encoding="utf-8", newline="\n")


def _read_manifest(path: Path) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError as exc:
        raise SnapshotValidationError(f"manifest missing: {path}") from exc
    for index, line in enumerate(lines, start=1):
        parts = line.split("\t", maxsplit=2)
        if len(parts) != 3:
            raise SnapshotValidationError(f"invalid manifest line {index}")
        sha256, raw_size, relative_path = parts
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise SnapshotValidationError(f"invalid manifest size on line {index}") from exc
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise SnapshotValidationError(f"invalid manifest hash on line {index}")
        entries.append(ManifestEntry(relative_path, size, sha256))
    return entries


def _safe_snapshot_relative(snapshot: Path, relative_text: str) -> Path:
    relative = Path(relative_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise SnapshotValidationError(f"unsafe manifest path: {relative_text}")
    candidate = _resolved(snapshot / relative)
    if snapshot not in candidate.parents:
        raise SnapshotValidationError(f"manifest path escapes snapshot: {relative_text}")
    return candidate


def validate_snapshot(snapshot: Path) -> ValidationReport:
    snapshot = _resolved(snapshot)
    if not snapshot.is_dir():
        raise SnapshotValidationError(f"snapshot directory missing: {snapshot}")
    marker_path = snapshot / "COMPLETE.json"
    metadata_path = snapshot / "metadata.json"
    manifest_path = snapshot / "manifest.sha256"
    try:
        marker = json.loads(marker_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError("valid COMPLETE.json is required") from exc
    if marker.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotValidationError("unsupported snapshot schema")
    if marker.get("manifest_sha256") != _hash_optional(manifest_path):
        raise SnapshotValidationError("manifest marker hash mismatch")
    if marker.get("metadata_sha256") != _hash_optional(metadata_path):
        raise SnapshotValidationError("metadata marker hash mismatch")

    entries = _read_manifest(manifest_path)
    expected: set[str] = set()
    total_bytes = 0
    for entry in entries:
        if not entry.relative_path.startswith("data/"):
            raise SnapshotValidationError(f"manifest path is not data: {entry.relative_path}")
        data_relative = Path(entry.relative_path.removeprefix("data/"))
        if exclusion_reason(data_relative):
            raise SnapshotValidationError(
                f"sensitive or transient file found in manifest: {entry.relative_path}"
            )
        path = _safe_snapshot_relative(snapshot, entry.relative_path)
        if not path.is_file():
            raise SnapshotValidationError(f"manifest file missing: {entry.relative_path}")
        if path.stat().st_size != entry.size:
            raise SnapshotValidationError(f"size mismatch: {entry.relative_path}")
        if _hash_file(path) != entry.sha256:
            raise SnapshotValidationError(f"hash mismatch: {entry.relative_path}")
        expected.add(entry.relative_path)
        total_bytes += entry.size

    actual = {
        f"data/{path.relative_to(snapshot / 'data').as_posix()}"
        for path in (snapshot / "data").rglob("*")
        if path.is_file()
    }
    extras = sorted(actual - expected)
    if extras:
        raise SnapshotValidationError(f"unmanifested payload files: {extras[:3]}")
    if marker.get("file_count") != len(entries):
        raise SnapshotValidationError("COMPLETE file count mismatch")
    if marker.get("total_bytes") != total_bytes:
        raise SnapshotValidationError("COMPLETE byte count mismatch")
    return ValidationReport(snapshot, len(entries), total_bytes)


@contextmanager
def _exclusive_backup_lock(backup_root: Path):
    lock_path = backup_root / ".backup-run.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(
            f"backup lock already exists; inspect before removing manually: {lock_path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "pid": os.getpid(),
                    "started_at_utc": datetime.now(UTC).isoformat(),
                },
                handle,
            )
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _snapshot_datetime(path: Path) -> datetime | None:
    match = _SNAPSHOT_RE.fullmatch(path.name)
    if not match:
        return None
    return datetime.strptime(
        match.group("date") + match.group("time"), "%Y%m%d%H%M%S"
    )


def _safe_remove_snapshot(path: Path, backup_root: Path) -> None:
    resolved_path = _resolved(path)
    resolved_root = _resolved(backup_root)
    if resolved_path.parent != resolved_root:
        raise ValueError(f"refusing to delete outside backup root: {resolved_path}")
    if _snapshot_datetime(resolved_path) is None or not (resolved_path / "COMPLETE.json").is_file():
        raise ValueError(f"refusing to delete unrecognized snapshot: {resolved_path}")
    validate_snapshot(resolved_path)
    shutil.rmtree(resolved_path)


def apply_retention_policy(
    backup_root: Path, *, daily: int = 14, weekly: int = 8, monthly: int = 12
) -> list[Path]:
    """Rotate only verified ``*.complete`` snapshots; preserve all other paths."""
    backup_root = _resolved(backup_root)
    candidates: list[tuple[datetime, Path]] = []
    for path in backup_root.iterdir():
        timestamp = _snapshot_datetime(path)
        if timestamp is None or not (path / "COMPLETE.json").is_file():
            continue
        try:
            validate_snapshot(path)
        except SnapshotValidationError:
            continue
        candidates.append((timestamp, path))
    candidates.sort(key=lambda item: item[0], reverse=True)

    keep: set[Path] = set()
    daily_seen: set[tuple[int, int, int]] = set()
    for timestamp, path in candidates:
        key = (timestamp.year, timestamp.month, timestamp.day)
        if len(daily_seen) < max(daily, 0) and key not in daily_seen:
            daily_seen.add(key)
            keep.add(path)

    weekly_seen: set[tuple[int, int]] = set()
    for timestamp, path in candidates:
        if timestamp.weekday() != 6:
            continue
        iso = timestamp.isocalendar()
        key = (iso.year, iso.week)
        if len(weekly_seen) < max(weekly, 0) and key not in weekly_seen:
            weekly_seen.add(key)
            keep.add(path)

    first_sundays: dict[tuple[int, int], tuple[datetime, Path]] = {}
    for timestamp, path in sorted(candidates, key=lambda item: item[0]):
        if timestamp.weekday() == 6:
            first_sundays.setdefault((timestamp.year, timestamp.month), (timestamp, path))
    for _timestamp, path in sorted(first_sundays.values(), reverse=True)[: max(monthly, 0)]:
        keep.add(path)

    deleted: list[Path] = []
    for _timestamp, path in candidates:
        if path in keep:
            continue
        _safe_remove_snapshot(path, backup_root)
        deleted.append(path)
    return deleted


def create_backup(
    *,
    project_root: Path = DEFAULT_PROJECT_ROOT,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    lifecycle: ContainerLifecycle | None = None,
    apply_retention: bool = True,
) -> Path:
    project_root, backup_root, data_dir = _validate_roots(project_root, backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    lifecycle = lifecycle or DockerComposeLifecycle(project_root)
    started_utc = datetime.now(UTC)
    local_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    unique = uuid.uuid4().hex[:8]
    staging = backup_root / f"tickflow-{local_stamp}-{unique}.staging"
    complete = backup_root / f"tickflow-{local_stamp}-{unique}.complete"
    snapshot_created = False

    with _exclusive_backup_lock(backup_root):
        initial = lifecycle.inspect()
        if not initial.exists:
            raise RuntimeError(
                f"{CONTAINER_NAME} does not exist; refuse an uncoordinated hot backup"
            )
        was_running = initial.running
        primary_error: BaseException | None = None
        try:
            if was_running:
                lifecycle.stop()
            staging.mkdir()
            entries, excluded = _copy_source_files(data_dir, staging)
            manifest_path = staging / "manifest.sha256"
            metadata_path = staging / "metadata.json"
            _write_manifest(manifest_path, entries)
            metadata = {
                "schema_version": SCHEMA_VERSION,
                "source_data_dir": str(data_dir),
                "project_root": str(project_root),
                "started_at_utc": started_utc.isoformat(),
                "completed_at_utc": datetime.now(UTC).isoformat(),
                "excluded_rules": {
                    "transient_suffixes": list(_TRANSIENT_SUFFIXES),
                    "private_key_suffixes": list(_PRIVATE_KEY_SUFFIXES),
                    "credential_like_names": True,
                    "credential_like_content": True,
                },
                "excluded_files": excluded,
                "container": {
                    **asdict(initial),
                    "was_running": was_running,
                },
                "compose_sha256": _hash_optional(project_root / "docker-compose.yml"),
                "tiers_sha256": _hash_optional(project_root / "tiers.yaml"),
                "file_count": len(entries),
                "total_bytes": sum(entry.size for entry in entries),
            }
            _write_json_atomic(metadata_path, metadata)
            marker = {
                "schema_version": SCHEMA_VERSION,
                "completed_at_utc": datetime.now(UTC).isoformat(),
                "manifest_sha256": _hash_file(manifest_path),
                "metadata_sha256": _hash_file(metadata_path),
                "file_count": len(entries),
                "total_bytes": sum(entry.size for entry in entries),
            }
            _write_json_atomic(staging / "COMPLETE.json", marker)
            validate_snapshot(staging)
            staging.rename(complete)
            snapshot_created = True
        except BaseException as exc:  # preserve original error after container recovery
            primary_error = exc
        finally:
            if was_running:
                try:
                    lifecycle.start_and_wait_healthy()
                except BaseException as restart_error:
                    if primary_error is not None:
                        raise ExceptionGroup(
                            "backup failed and TickFlow container recovery also failed",
                            [primary_error, restart_error],
                        ) from restart_error
                    raise
        if primary_error is not None:
            raise primary_error
        if not snapshot_created:
            raise RuntimeError("backup did not publish a complete snapshot")
        validate_snapshot(complete)
        if apply_retention:
            apply_retention_policy(backup_root)
    return complete


def restore_snapshot(
    snapshot: Path, *, destination: Path, production_data_dir: Path
) -> Path:
    snapshot = _resolved(snapshot)
    destination = _resolved(destination)
    production_data_dir = _resolved(production_data_dir)
    if destination == production_data_dir:
        raise ValueError("refusing to restore over production data")
    if destination in production_data_dir.parents or production_data_dir in destination.parents:
        raise ValueError("restore destination must be isolated from production data")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"restore destination must be empty: {destination}")
    validate_snapshot(snapshot)
    payload = snapshot / "data"
    destination.mkdir(parents=True, exist_ok=True)
    for source in payload.rglob("*"):
        relative = source.relative_to(payload)
        target = destination / relative
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return destination


def _write_last_run(backup_root: Path, payload: dict) -> None:
    backup_root.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(backup_root / "last-run.json", payload)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    backup_parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("snapshot", type=Path)
    restore_parser = subparsers.add_parser("restore-test")
    restore_parser.add_argument("snapshot", type=Path)
    restore_parser.add_argument("destination", type=Path)
    restore_parser.add_argument("--production-data", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "backup":
        try:
            snapshot = create_backup(
                project_root=args.project_root,
                backup_root=args.backup_root,
            )
            report = validate_snapshot(snapshot)
            payload = {
                "ok": True,
                "completed_at_utc": datetime.now(UTC).isoformat(),
                "snapshot": str(snapshot),
                "file_count": report.file_count,
                "total_bytes": report.total_bytes,
            }
            _write_last_run(args.backup_root, payload)
            print(json.dumps(payload, ensure_ascii=False))
            return 0
        except BaseException as exc:
            payload = {
                "ok": False,
                "completed_at_utc": datetime.now(UTC).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            }
            _write_last_run(args.backup_root, payload)
            print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
            return 1
    if args.command == "verify":
        report = validate_snapshot(args.snapshot)
        print(json.dumps({**asdict(report), "path": str(report.path)}, ensure_ascii=False))
        return 0
    restored = restore_snapshot(
        args.snapshot,
        destination=args.destination,
        production_data_dir=args.production_data,
    )
    print(json.dumps({"ok": True, "restored": str(restored)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
