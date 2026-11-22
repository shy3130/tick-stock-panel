import errno
from concurrent.futures import ThreadPoolExecutor

import pytest
from tivelo_contracts.artifacts import SnapshotBundle
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_data.storage import file_digest


def snapshot(engine, plan):
    return engine.create_snapshot((engine.publish(engine.sync(plan)).version_id,))


@pytest.mark.parametrize("phase", ["exporting", "publishing"])
def test_observed_cancel_never_publishes_and_releases_source(
    make_engine, make_bar, tmp_path, phase
):
    class Observer:
        stopped = False

        def cancelled(self):
            return self.stopped

        def progress(self, done, total, stage):
            if stage == phase:
                self.stopped = True

    engine, _, plan = make_engine((make_bar(),))
    original = snapshot(engine, plan)
    destination = tmp_path / "cancelled"
    observer = Observer()
    with pytest.raises(TiveloError) as error:
        engine.export_snapshot(original.snapshot_id, destination, observer=observer)
    assert observer.stopped
    assert error.value.code == ErrorCode.CANCELLED
    assert not destination.exists()
    assert not list(tmp_path.glob(".tivelo-export-*"))
    engine.store.delete_snapshot(original.snapshot_id)


def test_portable_export_contains_only_locked_facts_and_retains_snapshot_identity(
    make_engine, make_bar, tmp_path
):
    engine, _, plan = make_engine((make_bar(),))
    original = snapshot(engine, plan)
    target = tmp_path / "portable"
    bundle = engine.export_snapshot(original.snapshot_id, target)
    assert bundle.snapshot == original
    assert SnapshotBundle.model_validate_json((target / "bundle.json").read_bytes()) == bundle
    assert (target / "bundle.json").read_bytes() == canonical_bytes(bundle)
    expected = {"bundle.json"}
    for dataset in bundle.datasets:
        for artifact in dataset.contents.artifacts:
            copy = target / artifact.path
            expected.add(artifact.path)
            assert file_digest(copy) == artifact.sha256
            assert not copy.samefile(engine.store.path(artifact.path))
    assert {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()} == expected
    # Once exported, the bundle is independent of the source index and its pins.
    engine.store.delete_snapshot(original.snapshot_id)
    assert SnapshotBundle.model_validate_json((target / "bundle.json").read_bytes()) == bundle


def test_identical_export_is_idempotent_but_existing_other_content_is_never_replaced(
    make_engine, make_bar, tmp_path
):
    engine, provider, plan = make_engine((make_bar(),))
    original = snapshot(engine, plan)
    target = tmp_path / "portable"
    bundle = engine.export_snapshot(original.snapshot_id, target)
    timestamp = (target / "bundle.json").stat().st_mtime_ns
    assert engine.export_snapshot(original.snapshot_id, target) == bundle
    assert (target / "bundle.json").stat().st_mtime_ns == timestamp
    provider.records = (make_bar(close="12"),)
    newer = snapshot(engine, plan)
    with pytest.raises(TiveloError) as raised:
        engine.export_snapshot(newer.snapshot_id, target)
    assert raised.value.code == ErrorCode.DATA_CONFLICT
    assert (target / "bundle.json").read_bytes() == canonical_bytes(bundle)
    unknown = tmp_path / "existing-empty-directory"
    unknown.mkdir()
    with pytest.raises(TiveloError):
        engine.export_snapshot(original.snapshot_id, unknown)
    assert unknown.is_dir() and list(unknown.iterdir()) == []


def test_export_rejects_corrupt_existing_or_source_facts(make_engine, make_bar, tmp_path):
    engine, _, plan = make_engine((make_bar(),))
    original = snapshot(engine, plan)
    target = tmp_path / "portable"
    bundle = engine.export_snapshot(original.snapshot_id, target)
    artifact = bundle.datasets[0].contents.artifacts[0]
    (target / artifact.path).write_bytes(b"corrupt")
    with pytest.raises(TiveloError) as raised:
        engine.export_snapshot(original.snapshot_id, target)
    assert raised.value.code == ErrorCode.SNAPSHOT_CORRUPT
    engine.store.path(artifact.path).write_bytes(b"corrupt source")
    with pytest.raises(TiveloError) as raised:
        engine.export_snapshot(original.snapshot_id, tmp_path / "new")
    assert raised.value.code == ErrorCode.SNAPSHOT_CORRUPT
    assert not (tmp_path / "new").exists()


def test_copy_failure_never_publishes_a_bundle_and_releases_reference(
    make_engine, make_bar, tmp_path, monkeypatch
):
    import tivelo_data.export as exporting

    engine, _, plan = make_engine((make_bar(),))
    original = snapshot(engine, plan)
    target = tmp_path / "portable"

    def fail(source, destination):
        destination.write(b"partial")
        raise OSError(errno.ENOSPC, "simulated disk full")

    monkeypatch.setattr(exporting.shutil, "copyfileobj", fail)
    with pytest.raises(TiveloError) as raised:
        engine.export_snapshot(original.snapshot_id, target)
    assert raised.value.code == ErrorCode.PUBLICATION_FAILED
    assert not target.exists()
    assert not list(tmp_path.glob(".tivelo-export-*"))
    engine.store.delete_snapshot(original.snapshot_id)


def test_export_pins_source_during_copy_without_holding_index_transaction(
    make_engine, make_bar, tmp_path, monkeypatch
):
    import tivelo_data.export as exporting

    engine, _, plan = make_engine((make_bar(),))
    original = snapshot(engine, plan)
    copy = exporting.shutil.copyfileobj

    def checked_copy(source, destination):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(engine.store.delete_snapshot, original.snapshot_id)
            with pytest.raises(TiveloError) as raised:
                pending.result(timeout=2)
            assert raised.value.code == ErrorCode.REFERENCED_ARTIFACT
        copy(source, destination)

    monkeypatch.setattr(exporting.shutil, "copyfileobj", checked_copy)
    engine.export_snapshot(original.snapshot_id, tmp_path / "portable")
    engine.store.delete_snapshot(original.snapshot_id)


def test_export_refuses_symbolic_link_target(make_engine, make_bar, tmp_path):
    engine, _, plan = make_engine((make_bar(),))
    original = snapshot(engine, plan)
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links unavailable on this platform")
    with pytest.raises(TiveloError):
        engine.export_snapshot(original.snapshot_id, alias)
    assert list(real.iterdir()) == []


def test_publication_failure_preserves_unknown_destination_content(
    make_engine, make_bar, tmp_path, monkeypatch
):
    import tivelo_data.export as exporting

    engine, _, plan = make_engine((make_bar(),))
    original = snapshot(engine, plan)
    target = tmp_path / "portable"
    link = exporting.os.link

    def fail_marker(source, destination):
        if destination.name == "bundle.json":
            (target / "user-file.txt").write_text("keep me", encoding="utf-8")
            raise OSError(errno.ENOSPC, "simulated publication failure")
        return link(source, destination)

    monkeypatch.setattr(exporting.os, "link", fail_marker)
    with pytest.raises(TiveloError) as raised:
        engine.export_snapshot(original.snapshot_id, target)
    assert raised.value.code == ErrorCode.PUBLICATION_FAILED
    assert list(target.iterdir()) == [target / "user-file.txt"]
    assert (target / "user-file.txt").read_text(encoding="utf-8") == "keep me"
    engine.store.delete_snapshot(original.snapshot_id)
