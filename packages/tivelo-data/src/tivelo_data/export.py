"""Portable, self-contained snapshots; bundle.json is the publication marker.

Facts are copied and verified in a sibling staging directory. The destination
is claimed exclusively, then immutable files are linked into it; the manifest
is published last. No existing file is replaced. An interrupted process may
leave an incomplete directory, which readers must reject rather than repair.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

from pydantic import ValidationError
from tivelo_contracts.artifacts import MAX_SNAPSHOT_BUNDLE_BYTES, SnapshotBundle
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError

from .storage import _sync_directory, durable_mkdir, file_digest


def _safe_file(root, relative):
    path = root
    for part in Path(relative).parts:
        path /= part
        if path.is_symlink():
            raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Bundle contains a symbolic link")
    if not stat.S_ISREG(path.stat().st_mode):
        raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Bundle fact is not a regular file")
    return path


def _verify_existing(target: Path, bundle: SnapshotBundle) -> SnapshotBundle:
    if target.is_symlink() or not target.is_dir():
        raise TiveloError(ErrorCode.DATA_CONFLICT, "Export destination already exists")
    try:
        manifest = _safe_file(target, "bundle.json")
        if manifest.stat().st_size > MAX_SNAPSHOT_BUNDLE_BYTES:
            raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Bundle manifest exceeds size limit")
        existing = SnapshotBundle.model_validate_json(manifest.read_bytes())
    except (OSError, ValidationError) as exc:
        raise TiveloError(ErrorCode.DATA_CONFLICT, "Destination is not a complete bundle") from exc
    if existing != bundle:
        raise TiveloError(ErrorCode.DATA_CONFLICT, "Destination contains a different snapshot")
    try:
        for dataset in bundle.datasets:
            for artifact in dataset.contents.artifacts:
                path = _safe_file(target, artifact.path)
                if (
                    path.stat().st_size != artifact.size_bytes
                    or file_digest(path) != artifact.sha256
                ):
                    raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Exported fact hash differs")
    except OSError as exc:
        raise TiveloError(ErrorCode.SNAPSHOT_CORRUPT, "Exported fact is unavailable") from exc
    return bundle


def export_snapshot(
    engine, snapshot_id: str, destination: str | Path, *, observer=None
) -> SnapshotBundle:
    def check_cancelled():
        if observer is not None and observer.cancelled():
            raise TiveloError(ErrorCode.CANCELLED, "Snapshot export cancelled before publication")

    requested = Path(destination).expanduser().absolute()
    target = requested.parent.resolve() / requested.name
    created_files = []
    created_directories = []
    published = False
    try:
        check_cancelled()
        with engine.open_reader(snapshot_id) as reader:
            bundle = SnapshotBundle(snapshot=reader.manifest, datasets=reader.datasets)
            encoded = canonical_bytes(bundle)
            if len(encoded) > MAX_SNAPSHOT_BUNDLE_BYTES:
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Bundle manifest exceeds size limit")
            if target.exists() or target.is_symlink():
                return _verify_existing(target, bundle)
            reader.verify()
            durable_mkdir(target.parent)
            with tempfile.TemporaryDirectory(prefix=".tivelo-export-", dir=target.parent) as temp:
                stage = Path(temp)
                staged = {}
                for dataset in bundle.datasets:
                    for artifact in dataset.contents.artifacts:
                        check_cancelled()
                        if artifact.path in staged:
                            continue
                        copy = stage / artifact.path
                        copy.parent.mkdir(parents=True, exist_ok=True)
                        with (
                            engine.store.path(artifact.path).open("rb") as source,
                            copy.open("xb") as output,
                        ):
                            if observer is None:
                                shutil.copyfileobj(source, output)
                            else:
                                copied = 0
                                while block := source.read(1024 * 1024):
                                    check_cancelled()
                                    output.write(block)
                                    copied += len(block)
                                    observer.progress(copied, artifact.size_bytes, "exporting")
                            output.flush()
                            os.fsync(output.fileno())
                        if (
                            copy.stat().st_size != artifact.size_bytes
                            or file_digest(copy) != artifact.sha256
                        ):
                            raise TiveloError(
                                ErrorCode.SNAPSHOT_CORRUPT, "Source changed during snapshot export"
                            )
                        staged[artifact.path] = copy
                marker = stage / "bundle.json"
                with marker.open("xb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    target.mkdir()
                except FileExistsError:
                    return _verify_existing(target, bundle)
                created_directories.append(target)
                for relative, copy in staged.items():
                    check_cancelled()
                    destination_file = target / relative
                    current = target
                    for part in Path(relative).parts[:-1]:
                        current /= part
                        if current not in created_directories:
                            current.mkdir()
                            created_directories.append(current)
                    os.link(copy, destination_file)
                    created_files.append(destination_file)
                for directory in reversed(created_directories):
                    _sync_directory(directory)
                _sync_directory(target.parent)
                if observer is not None:
                    observer.progress(0, 1, "publishing")
                check_cancelled()
                # link() provides atomic no-replace publication on the same filesystem.
                os.link(marker, target / "bundle.json")
                published = True
                _sync_directory(target)
                return bundle
    except OSError as exc:
        raise TiveloError(ErrorCode.PUBLICATION_FAILED, "Snapshot export I/O failed") from exc
    finally:
        if not published:
            # Only undo paths created by this invocation; never remove unknown contents.
            for path in reversed(created_files):
                try:
                    path.unlink()
                except OSError:
                    pass
            for path in reversed(created_directories):
                try:
                    path.rmdir()
                except OSError:
                    pass
