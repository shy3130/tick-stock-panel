"""Immutable facts and atomic publication; no workbench paths or settings.

Only index rows committed by SQLite make manifests visible. A crash between a
file rename and the index commit may leave an orphan file, never a partial
published version. Source facts are not automatically garbage-collected.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from pydantic import Field, TypeAdapter, ValidationError
from tivelo_contracts.base import Contract, Digest, RelativePath, canonical_bytes
from tivelo_contracts.data import (
    DatasetReference,
    DatasetVersion,
    SnapshotContents,
    SnapshotManifest,
)
from tivelo_contracts.errors import ErrorCode, TiveloError

from .publications import initialize_journal

STORE_FORMAT = "tivelo-data-store-v1"
WRITER_VERSION = "0.1.0a1"


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def durable_mkdir(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        _sync_directory(directory.parent)



def _install_immutable(source: Path, destination: Path) -> bool:
    # Windows cannot replace a file another publisher has open. Rename
    # without replacing it there, and let the caller verify the existing bytes.
    try:
        source.rename(destination)
    except FileExistsError:
        return False
    _sync_directory(destination.parent)
    return True

class Candidate(Contract):
    candidate_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    version: DatasetVersion


class SnapshotStore:
    def __init__(self, root: str | Path):
        directory = Path(root).expanduser().resolve()
        durable_mkdir(directory)
        self.root = directory.resolve(strict=True)
        self.index = self.path("index.sqlite3")
        with self.transaction() as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables and "metadata" not in tables:
                raise TiveloError(ErrorCode.SCHEMA_UNSUPPORTED, "Unrecognized data index")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            values = dict(connection.execute("SELECT key, value FROM metadata"))
            if values and (
                values.get("format") != STORE_FORMAT or values.get("writer") != WRITER_VERSION
            ):
                raise TiveloError(
                    ErrorCode.SCHEMA_UNSUPPORTED,
                    "Store requires an explicit format/runtime migration",
                )
            if not values:
                connection.executemany(
                    "INSERT INTO metadata VALUES (?, ?)",
                    (("format", STORE_FORMAT), ("writer", WRITER_VERSION)),
                )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS versions (version_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, manifest TEXT NOT NULL)"
            )
            if ("publication_events" in tables) != ("publication_stream" in values):
                raise TiveloError(
                    ErrorCode.SNAPSHOT_CORRUPT, "Publication journal and store identity disagree"
                )
            initialize_journal(connection)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS snapshots (snapshot_id TEXT PRIMARY KEY, manifest TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS snapshot_versions (snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id), version_id TEXT NOT NULL REFERENCES versions(version_id), PRIMARY KEY(snapshot_id, version_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS refs (owner TEXT NOT NULL, snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id), PRIMARY KEY(owner, snapshot_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reader_refs (owner TEXT NOT NULL, snapshot_id TEXT NOT NULL, "
                "PRIMARY KEY(snapshot_id, owner), FOREIGN KEY(owner, snapshot_id) "
                "REFERENCES refs(owner, snapshot_id) ON DELETE CASCADE)"
            )

    def path(self, relative: str) -> Path:
        TypeAdapter(RelativePath).validate_python(relative)
        candidate = self.root / relative
        current = self.root
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise TiveloError(
                    ErrorCode.SNAPSHOT_CORRUPT, "Data paths must not contain symbolic links"
                )
        # Resolve an existing path so Windows never compares a partially
        # resolved name while another publisher creates the directory or file.
        existing = candidate
        while True:
            try:
                resolved = existing.resolve(strict=True)
                break
            except FileNotFoundError as exc:
                if existing == self.root:
                    raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Data store is unavailable") from exc
                existing = existing.parent
        if not resolved.is_relative_to(self.root):
            raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Data path escapes the store")
        return candidate

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(self.index, timeout=10, isolation_level=None)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error as exc:
            raise TiveloError(
                ErrorCode.PUBLICATION_FAILED, "Data index could not be opened"
            ) from exc
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                code = (
                    ErrorCode.RESOURCE_BUSY
                    if "locked" in str(exc).lower()
                    else ErrorCode.PUBLICATION_FAILED
                )
                raise TiveloError(code, "Data index transaction failed") from exc
            except BaseException:
                connection.rollback()
                raise

    def new_stage(self) -> tuple[str, Path]:
        operation = uuid.uuid4().hex
        directory = self.path(f"staging/{operation}")
        durable_mkdir(directory.parent)
        directory.mkdir(exist_ok=False)
        try:
            _sync_directory(directory.parent)
        except BaseException:
            directory.rmdir()
            raise
        return operation, directory

    def write_atomic(self, relative: str, content: bytes) -> None:
        destination = self.path(relative)
        durable_mkdir(destination.parent)
        if destination.exists():
            if destination.read_bytes() != content:
                raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Immutable manifest content differs")
            return
        temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if not _install_immutable(temporary, destination) and destination.read_bytes() != content:
                raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Immutable manifest content differs")
        finally:
            temporary.unlink(missing_ok=True)

    def save_candidate(self, candidate: Candidate) -> None:
        self.write_atomic(
            f"staging/{candidate.candidate_id}/candidate.json", canonical_bytes(candidate)
        )

    def candidate(self, candidate: Candidate) -> Candidate:
        file = self.path(f"staging/{candidate.candidate_id}/candidate.json")
        try:
            stored = Candidate.model_validate_json(file.read_bytes())
        except (OSError, ValueError) as exc:
            raise TiveloError(
                ErrorCode.SNAPSHOT_NOT_FOUND, "Staged candidate is unavailable"
            ) from exc
        if stored != candidate:
            raise TiveloError(
                ErrorCode.SNAPSHOT_CORRUPT, "Candidate differs from its staged manifest"
            )
        return stored

    def candidate_file(self, candidate: Candidate, index: int) -> Path:
        return self.path(f"staging/{candidate.candidate_id}/final-{index:05d}.parquet")

    def validate_candidate_files(self, candidate: Candidate) -> None:
        self.candidate(candidate)
        for index, artifact in enumerate(candidate.version.contents.artifacts):
            file = self.candidate_file(candidate, index)
            if (
                not file.is_file()
                or file.stat().st_size != artifact.size_bytes
                or file_digest(file) != artifact.sha256
            ):
                raise TiveloError(
                    ErrorCode.SNAPSHOT_CORRUPT, "Staged fact content failed verification"
                )

    def publish(self, candidate: Candidate) -> DatasetVersion:
        self.validate_candidate_files(candidate)
        version = candidate.version
        relative = f"dataset_versions/{version.version_id}/manifest.json"
        # Content-addressed files are invisible until the index commit. Copy and
        # hash outside the index lock so readers can pin existing snapshots.
        for index, artifact in enumerate(version.contents.artifacts):
            destination = self.path(artifact.path)
            durable_mkdir(destination.parent)
            if destination.exists():
                if file_digest(destination) != artifact.sha256:
                    raise TiveloError(
                        ErrorCode.SNAPSHOT_CORRUPT, "Existing immutable facts are corrupt"
                    )
            else:
                # Keep the staged file so failed publication can be retried.
                temporary = destination.with_name(
                    destination.name + "." + uuid.uuid4().hex + ".tmp"
                )
                try:
                    with (
                        self.candidate_file(candidate, index).open("rb") as source,
                        temporary.open("xb") as output,
                    ):
                        shutil.copyfileobj(source, output)
                        output.flush()
                        os.fsync(output.fileno())
                    if (
                        temporary.stat().st_size != artifact.size_bytes
                        or file_digest(temporary) != artifact.sha256
                    ):
                        raise TiveloError(
                            ErrorCode.SNAPSHOT_CORRUPT, "Staged facts changed during publication"
                        )
                    if (
                        not _install_immutable(temporary, destination)
                        and file_digest(destination) != artifact.sha256
                    ):
                        raise TiveloError(
                            ErrorCode.SNAPSHOT_CORRUPT, "Existing immutable facts are corrupt"
                        )
                finally:
                    temporary.unlink(missing_ok=True)
        self.write_atomic(relative, canonical_bytes(version))
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO versions VALUES (?, ?, ?)",
                (version.version_id, version.contents.dataset_id, relative),
            )
        return version

    def _load_version(self, connection: sqlite3.Connection, version_id: str) -> DatasetVersion:
        TypeAdapter(Digest).validate_python(version_id)
        row = connection.execute(
            "SELECT dataset_id, manifest FROM versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise TiveloError(ErrorCode.SNAPSHOT_NOT_FOUND, "Dataset version is not published")
        try:
            version = DatasetVersion.model_validate_json(self.path(row[1]).read_bytes())
        except (OSError, ValueError) as exc:
            raise TiveloError(
                ErrorCode.SNAPSHOT_CORRUPT, "Dataset manifest failed verification"
            ) from exc
        if version.version_id != version_id or version.contents.dataset_id != row[0]:
            raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Dataset index and manifest disagree")
        return version

    def dataset_version(self, version_id: str) -> DatasetVersion:
        with self.connection() as connection:
            return self._load_version(connection, version_id)

    def create_snapshot(self, version_ids: tuple[str, ...]) -> SnapshotManifest:
        with self.transaction() as connection:
            versions = [self._load_version(connection, value) for value in version_ids]
            contents = SnapshotContents(
                datasets=tuple(
                    sorted(
                        (
                            DatasetReference(
                                dataset_id=v.contents.dataset_id, version_id=v.version_id
                            )
                            for v in versions
                        ),
                        key=lambda value: value.dataset_id,
                    )
                )
            )
            snapshot = SnapshotManifest.create(contents)
            relative = f"snapshots/{snapshot.snapshot_id}/manifest.json"
            self.write_atomic(relative, canonical_bytes(snapshot))
            connection.execute(
                "INSERT OR IGNORE INTO snapshots VALUES (?, ?)", (snapshot.snapshot_id, relative)
            )
            connection.executemany(
                "INSERT OR IGNORE INTO snapshot_versions VALUES (?, ?)",
                ((snapshot.snapshot_id, v.version_id) for v in versions),
            )
        return snapshot

    def _load_snapshot(self, connection: sqlite3.Connection, snapshot_id: str) -> SnapshotManifest:
        TypeAdapter(Digest).validate_python(snapshot_id)
        row = connection.execute(
            "SELECT manifest FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise TiveloError(ErrorCode.SNAPSHOT_NOT_FOUND, "Snapshot is not published")
        try:
            snapshot = SnapshotManifest.model_validate_json(self.path(row[0]).read_bytes())
        except (OSError, ValidationError) as exc:
            raise TiveloError(
                ErrorCode.SNAPSHOT_CORRUPT, "Snapshot manifest failed verification"
            ) from exc
        indexed = {
            r[0]
            for r in connection.execute(
                "SELECT version_id FROM snapshot_versions WHERE snapshot_id=?", (snapshot_id,)
            )
        }
        if snapshot.snapshot_id != snapshot_id or indexed != {
            r.version_id for r in snapshot.contents.datasets
        }:
            raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Snapshot index and manifest disagree")
        return snapshot

    def snapshot(self, snapshot_id: str) -> SnapshotManifest:
        with self.connection() as connection:
            return self._load_snapshot(connection, snapshot_id)

    def pin(self, snapshot_id: str, owner: str) -> SnapshotManifest:
        if not owner or len(owner) > 256:
            raise ValueError("reference owner must be 1–256 characters")
        with self.transaction() as connection:
            snapshot = self._load_snapshot(connection, snapshot_id)
            connection.execute("INSERT OR IGNORE INTO refs VALUES (?, ?)", (owner, snapshot_id))
        return snapshot

    def release(self, snapshot_id: str, owner: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM refs WHERE owner=? AND snapshot_id=?", (owner, snapshot_id)
            )

    def _reader_guard(self, snapshot_id: str, *, exclusive: bool) -> sqlite3.Connection:
        # One empty rollback-mode database per snapshot gives each reader a
        # shared OS lock, independent of writes to the main data index.
        path = self.path(f"reader-locks/{snapshot_id}.sqlite3")
        durable_mkdir(path.parent)
        guard = sqlite3.connect(path, timeout=0, isolation_level=None, check_same_thread=False)
        try:
            if guard.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Unexpected snapshot reader lock mode")
            if exclusive:
                guard.execute("BEGIN EXCLUSIVE")
            else:
                guard.execute("BEGIN")
                guard.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            return guard
        except BaseException as exc:
            guard.close()
            if isinstance(exc, sqlite3.OperationalError) and getattr(exc, "sqlite_errorcode", 0) & 255 in (
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED
            ):
                code = ErrorCode.REFERENCED_ARTIFACT if exclusive else ErrorCode.RESOURCE_BUSY
                raise TiveloError(code, "Snapshot is in use") from exc
            raise

    @contextmanager
    def reader_reference(self, snapshot_id: str) -> Iterator[SnapshotManifest]:
        owner = "reader/" + uuid.uuid4().hex
        with ExitStack() as lifetime:
            with self.transaction() as connection:
                snapshot = self._load_snapshot(connection, snapshot_id)
                lifetime.enter_context(closing(self._reader_guard(snapshot.snapshot_id, exclusive=False)))
                # Older writers also see the protective reference. Only owners
                # recorded in reader_refs are eligible for automatic recovery.
                connection.execute("INSERT INTO refs VALUES (?,?)", (owner, snapshot_id))
                connection.execute("INSERT INTO reader_refs VALUES (?,?)", (owner, snapshot_id))
            try:
                yield snapshot
            finally:
                self.release(snapshot_id, owner)

    def delete_snapshot(self, snapshot_id: str) -> None:
        # Removing the index is enough to make a snapshot unavailable. Manifests
        # and source facts are retained; this is not an implicit user-data purge.
        with self.transaction() as connection:
            self._load_snapshot(connection, snapshot_id)
            with closing(self._reader_guard(snapshot_id, exclusive=True)):
                connection.execute(
                    "DELETE FROM refs WHERE snapshot_id=? AND owner IN "
                    "(SELECT owner FROM reader_refs WHERE snapshot_id=?)", (snapshot_id, snapshot_id)
                )
                if connection.execute(
                    "SELECT 1 FROM refs WHERE snapshot_id=? LIMIT 1", (snapshot_id,)
                ).fetchone():
                    raise TiveloError(
                        ErrorCode.REFERENCED_ARTIFACT, "Snapshot is referenced by a reader or task"
                    )
                connection.execute("DELETE FROM snapshot_versions WHERE snapshot_id=?", (snapshot_id,))
                connection.execute("DELETE FROM snapshots WHERE snapshot_id=?", (snapshot_id,))
