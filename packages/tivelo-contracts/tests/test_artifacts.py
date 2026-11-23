import pytest
from pydantic import ValidationError

from tivelo_contracts.artifacts import (
    ArchiveContents,
    ArchiveFile,
    ResultArchive,
    RunArchive,
    SnapshotBundle,
)
from tivelo_contracts.data import Artifact, DatasetVersion, SnapshotContents, SnapshotManifest
from tivelo_contracts.facts import RECORD_MODELS, fact_fields
from tivelo_contracts.testing import InMemoryReader


def bundle_for(make_bar, *, path=None):
    version = InMemoryReader(bars=(make_bar(),), knowledge_basis="historical_versions").datasets[0]
    digest = "a" * 64
    artifact = Artifact(
        path=path or f"datasets/aa/{digest}.parquet", sha256=digest, size_bytes=100, row_count=1
    )
    contents = version.contents.model_dump(mode="python")
    contents["artifacts"] = (artifact,)
    version = DatasetVersion.create(type(version.contents)(**contents))
    snapshot = SnapshotManifest.create(
        SnapshotContents(
            datasets=(
                {"dataset_id": version.contents.dataset_id, "version_id": version.version_id},
            )
        )
    )
    return snapshot, version


def test_bundle_requires_exact_referenced_datasets(make_bar):
    snapshot, version = bundle_for(make_bar)
    assert SnapshotBundle(snapshot=snapshot, datasets=(version,)).snapshot == snapshot
    for values in ((), (version, version)):
        with pytest.raises(ValidationError):
            SnapshotBundle(snapshot=snapshot, datasets=values)


@pytest.mark.parametrize("path", ["bundle.json", "custom.parquet", "datasets/aa/wrong.parquet"])
def test_bundle_only_transports_content_addressed_parquet(make_bar, path):
    snapshot, version = bundle_for(make_bar, path=path)
    with pytest.raises(ValidationError):
        SnapshotBundle(snapshot=snapshot, datasets=(version,))


def test_physical_field_contract_preserves_order_precision_and_nested_nullability():
    for kind, model in RECORD_MODELS.items():
        assert [field.name for field in fact_fields(kind)] == list(model.model_fields)
    bar = {field.name: field for field in fact_fields("bar")}
    assert bar["close"].kind == "decimal128" and not bar["close"].nullable
    assert bar["turnover"].nullable
    assert bar["bar_start"].kind == "timestamp"
    listing = next(field for field in fact_fields("instrument") if field.name == "listing_date")
    assert listing.kind == "struct"
    assert [(f.name, f.kind, f.nullable) for f in listing.children] == [
        ("state", "string", False),
        ("value", "date", True),
    ]


def test_archive_fingerprint_and_required_members():
    contents = ArchiveContents(
        lock_id="a" * 64,
        files=(ArchiveFile(path="run-lock.json", sha256="b" * 64, size_bytes=1),),
    )
    manifest = RunArchive.create(contents)
    assert RunArchive.model_validate_json(manifest.model_dump_json()) == manifest
    with pytest.raises(ValidationError):
        RunArchive(archive_id="c" * 64, contents=contents)
    with pytest.raises(ValidationError):
        ResultArchive.create(contents)


def test_result_format_and_columnar_encoding_are_an_explicit_compatible_pair():
    paths = (
        "diagnostic-events.parquet",
        "equity.parquet",
        "ledger.parquet",
        "metrics.json",
        "orders.parquet",
        "run-lock.json",
        "summary.json",
    )
    contents = ArchiveContents(
        lock_id="a" * 64,
        files=tuple(
            ArchiveFile(
                path=path,
                sha256="b" * 64,
                size_bytes=1,
                row_count=0 if path.endswith(".parquet") else None,
            )
            for path in paths
        ),
    )
    for encoding, version in (("parquet-v1", "1.0"), ("parquet-v2", "1.1")):
        archive = ResultArchive.create(contents, encoding=encoding)
        assert archive.format_version == version
        assert ResultArchive.model_validate_json(archive.model_dump_json()) == archive
        invalid = archive.model_dump()
        invalid["format_version"] = "1.1" if version == "1.0" else "1.0"
        with pytest.raises(ValidationError):
            ResultArchive.model_validate(invalid)


@pytest.mark.parametrize(
    "paths",
    [
        ["manifest.json", "run-lock.json"],
        ["run-lock.json", "run-lock.json"],
        ["RUN-LOCK.json", "run-lock.json"],
        ["a", "a/b", "run-lock.json"],
        ["../escape", "run-lock.json"],
        ["CON/file", "run-lock.json"],
    ],
)
def test_archive_rejects_ambiguous_or_nonportable_paths(paths):
    with pytest.raises(ValidationError):
        ArchiveContents(
            lock_id="a" * 64,
            files=tuple(ArchiveFile(path=path, sha256="b" * 64, size_bytes=0) for path in paths),
        )
