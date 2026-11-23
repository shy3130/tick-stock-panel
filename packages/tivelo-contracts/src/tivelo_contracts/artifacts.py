"""Interoperable transport manifests; filenames never imply publication/trust."""

from typing import Literal, Self

from pydantic import Field, model_validator

from .base import Contract, Digest, fingerprint
from .data import DatasetVersion, SnapshotManifest
from .definitions import DefinitionFile

MAX_SNAPSHOT_BUNDLE_BYTES = 16 * 1024 * 1024


class SnapshotBundle(Contract):
    format: Literal["tivelo-snapshot-bundle"] = "tivelo-snapshot-bundle"
    format_version: Literal["1.0"] = "1.0"
    encoding: Literal["parquet-v2"] = "parquet-v2"
    snapshot: SnapshotManifest
    datasets: tuple[DatasetVersion, ...]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        identities = [(d.contents.dataset_id, d.version_id) for d in self.datasets]
        expected = [(r.dataset_id, r.version_id) for r in self.snapshot.contents.datasets]
        if identities != expected:
            raise ValueError("bundle datasets must exactly match the sorted snapshot manifest")
        paths = {}
        for dataset in self.datasets:
            for artifact in dataset.contents.artifacts:
                expected_path = f"datasets/{artifact.sha256[:2]}/{artifact.sha256}.parquet"
                if artifact.path != expected_path:
                    raise ValueError("bundle facts must use content-addressed Parquet paths")
                identity = (dataset.contents.record_type, dataset.contents.price_basis, artifact)
                if artifact.path in paths and paths[artifact.path] != identity:
                    raise ValueError("bundle artifact path has conflicting identities")
                paths[artifact.path] = identity
        return self


class ArchiveFile(DefinitionFile):
    """Portable relative filename with a content digest; row count for tables only."""

    row_count: int | None = Field(default=None, strict=True, ge=0)


class ArchiveContents(Contract):
    lock_id: Digest
    files: tuple[ArchiveFile, ...] = Field(min_length=1, max_length=8192)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        paths = [file.path for file in self.files]
        folded = {path.casefold() for path in paths}
        if paths != sorted(set(paths)) or len(folded) != len(paths):
            raise ValueError("archive paths must be unique, portable and sorted")
        if "manifest.json" in paths or "run-lock.json" not in paths:
            raise ValueError("archive must contain a run lock and cannot include its own marker")
        for path in paths:
            parts = path.split("/")
            if any("/".join(parts[:index]).casefold() in folded for index in range(1, len(parts))):
                raise ValueError("archive file conflicts with a directory")
        return self


class RunArchive(Contract):
    format: Literal["tivelo-run-archive"] = "tivelo-run-archive"
    format_version: Literal["1.0"] = "1.0"
    archive_id: Digest
    contents: ArchiveContents

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.archive_id != fingerprint(self.contents):
            raise ValueError("run archive fingerprint mismatch")
        return self

    @classmethod
    def create(cls, contents: ArchiveContents) -> Self:
        return cls(archive_id=fingerprint(contents), contents=contents)


class ResultArchive(Contract):
    format: Literal["tivelo-result-archive"] = "tivelo-result-archive"
    format_version: Literal["1.0", "1.1"] = "1.0"
    encoding: Literal["parquet-v1", "parquet-v2"] = "parquet-v1"
    archive_id: Digest
    contents: ArchiveContents

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.format_version, self.encoding) not in (
            ("1.0", "parquet-v1"),
            ("1.1", "parquet-v2"),
        ):
            raise ValueError("result encoding does not match its format version")
        if self.archive_id != fingerprint(self.contents):
            raise ValueError("result archive fingerprint mismatch")
        required = {
            "summary.json",
            "metrics.json",
            "orders.parquet",
            "equity.parquet",
            "ledger.parquet",
            "diagnostic-events.parquet",
        }
        if not required <= {file.path for file in self.contents.files}:
            raise ValueError("result archive is incomplete")
        tables = {"orders.parquet", "equity.parquet", "ledger.parquet", "diagnostic-events.parquet"}
        if any(
            (file.row_count is not None) != (file.path in tables) for file in self.contents.files
        ):
            raise ValueError("only result tables have row counts")
        return self

    @classmethod
    def create(
        cls,
        contents: ArchiveContents,
        *,
        encoding: Literal["parquet-v1", "parquet-v2"] = "parquet-v1",
    ) -> Self:
        return cls(
            format_version="1.0" if encoding == "parquet-v1" else "1.1",
            encoding=encoding,
            archive_id=fingerprint(contents),
            contents=contents,
        )
