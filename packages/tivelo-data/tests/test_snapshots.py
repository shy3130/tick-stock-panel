import errno
import multiprocessing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from threading import Event

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tivelo_contracts import ColumnarDataReader, DataReader, ErrorCode, TiveloError
from tivelo_contracts.data import BarQuery, Coverage

from tivelo_data.providers import SyncContext, local_context
from tivelo_data.storage import SnapshotStore


def _reader_process(root, snapshot_id, channel):
    from tivelo_data import DataEngine

    with DataEngine(root).open_reader(snapshot_id) as reader:
        channel.send(reader.manifest.snapshot_id)
        channel.recv()


def _publish_before_commit(root, payload, operation, channel):
    from tivelo_data import DataEngine
    from tivelo_data.storage import Candidate

    engine = DataEngine(root)
    candidate = Candidate.model_validate_json(payload)
    transaction = engine.store.transaction

    @contextmanager
    def wait_before_commit():
        with transaction() as db:
            yield db
            version_id = candidate.version.version_id
            if operation == "snapshot":
                (identity,) = db.execute(
                    "SELECT snapshot_id FROM snapshot_versions WHERE version_id=?", (version_id,)
                ).fetchone()
            else:
                (identity,) = db.execute(
                    "SELECT version_id FROM versions WHERE version_id=?", (version_id,)
                ).fetchone()
            channel.send(identity)
            channel.recv()

    engine.store.transaction = wait_before_commit
    if operation == "version":
        engine.publish(candidate)
    else:
        engine.create_snapshot((candidate.version.version_id,))


def query(as_of="2025-01-03T00:00:00Z"):
    return BarQuery(
        instrument_ids=("security-1",),
        timeframe="1d",
        start="2025-01-01T00:00:00Z",
        end="2025-02-01T00:00:00Z",
        as_of=as_of,
    )


def publish(engine, plan):
    candidate = engine.sync(plan)
    version = engine.publish(candidate)
    return candidate, version, engine.create_snapshot((version.version_id,))


def test_published_snapshot_implements_the_public_reader_vectors(make_engine):
    from tivelo_contracts.conformance import reader_fixture, verify_reader

    kinds = {
        "bars": "bar",
        "instruments": "instrument",
        "actions": "corporate_action",
        "factors": "adjustment_factor",
        "openings": "opening_observation",
    }
    versions = []
    for name, rows in reader_fixture().items():
        engine, _, plan = make_engine(rows, record_type=kinds[name])
        versions.append(engine.publish(engine.sync(plan)).version_id)
    snapshot = engine.create_snapshot(tuple(versions))
    with engine.open_reader(snapshot.snapshot_id) as reader:
        assert len(verify_reader(reader, execution=True)) == 18


def test_d04_independent_full_pipeline_preserves_decimal_utc_and_missing_values(
    make_bar, make_engine
):
    bar = make_bar(close="11.123456789123", turnover=None)
    engine, provider, plan = make_engine((bar,))
    assert provider.calls == 0
    candidate = engine.sync(plan)
    with pytest.raises(TiveloError):
        engine.store.dataset_version(candidate.version.version_id)
    assert engine.validate(candidate) == candidate.version
    physical = pq.read_table(engine.store.candidate_file(candidate, 0))
    assert physical.schema.field("close").type == pa.decimal128(38, 12)
    assert physical.schema.field("bar_start").type == pa.timestamp("us", tz="UTC")
    assert physical["close"].to_pylist() == [Decimal("11.123456789123")]
    assert physical["turnover"].to_pylist() == [None]
    version = engine.publish(candidate)
    snapshot = engine.create_snapshot((version.version_id,))
    with engine.open_reader(snapshot.snapshot_id) as reader:
        assert isinstance(reader, DataReader)
        assert reader.bars(query()) == (bar,)
        assert reader.datasets[0].contents.scope.coverage_basis == "source_response"
    assert provider.closed == 1


def test_d07_pagination_and_order_do_not_change_version_identity(make_bar, make_engine, tmp_path):
    records = (
        make_bar(),
        make_bar(instrument_id="security-2"),
        make_bar(instrument_id="security-3"),
    )
    ids = []
    for size, ordered in ((1, records), (2, records[::-1]), (100, records)):
        engine, _, plan = make_engine(ordered, page_size=size, directory=tmp_path / str(size))
        _, version, snapshot = publish(engine, plan)
        ids.append((version.version_id, snapshot.snapshot_id))
    assert len(set(ids)) == 1


def test_d07_later_correction_preserves_old_snapshot_and_knowledge_boundary(make_bar, make_engine):
    original = make_bar()
    engine, provider, plan = make_engine((original,))
    _, old_version, old_snapshot = publish(engine, plan)
    revised = make_bar(
        close="12",
        revision_id="r2",
        available_at="2025-01-04T00:00:00Z",
        ingested_at="2025-01-04T00:01:00Z",
    )
    provider.records = (original, revised)
    _, new_version, new_snapshot = publish(engine, plan)
    assert old_version.version_id != new_version.version_id
    with (
        engine.open_reader(old_snapshot.snapshot_id) as old,
        engine.open_reader(new_snapshot.snapshot_id) as new,
    ):
        assert old.bars(query("2025-01-05T00:00:00Z")) == (original,)
        assert new.bars(query()) == (original,)
        assert new.bars(query("2025-01-05T00:00:00Z")) == (revised,)


def test_d08_manifest_failure_never_publishes_half_a_version(make_bar, make_engine, monkeypatch):
    engine, _, plan = make_engine((make_bar(),))
    candidate = engine.sync(plan)
    original_write = engine.store.write_atomic

    def fail(relative, content):
        if relative.startswith("dataset_versions/"):
            raise OSError(errno.ENOSPC, "simulated disk full")
        original_write(relative, content)

    monkeypatch.setattr(engine.store, "write_atomic", fail)
    with pytest.raises(OSError):
        engine.publish(candidate)
    restarted = SnapshotStore(engine.store.root)
    with pytest.raises(TiveloError) as raised:
        restarted.dataset_version(candidate.version.version_id)
    assert raised.value.code == ErrorCode.SNAPSHOT_NOT_FOUND
    monkeypatch.setattr(engine.store, "write_atomic", original_write)
    assert engine.publish(candidate) == candidate.version


def test_d08_snapshot_failure_keeps_previous_snapshot_visible(make_bar, make_engine, monkeypatch):
    engine, _, plan = make_engine((make_bar(),))
    _, version, snapshot = publish(engine, plan)
    original_write = engine.store.write_atomic

    def fail(relative, content):
        if relative.startswith("snapshots/"):
            raise OSError(errno.ENOSPC, "simulated disk full")
        original_write(relative, content)

    monkeypatch.setattr(engine.store, "write_atomic", fail)
    with pytest.raises(OSError):
        engine.create_snapshot((version.version_id,))
    assert engine.store.snapshot(snapshot.snapshot_id) == snapshot


@pytest.mark.parametrize("operation", ["version", "snapshot"])
def test_d08_killed_publisher_before_index_commit_is_invisible_and_can_resume(
    make_bar, make_engine, operation
):
    original = make_bar()
    engine, provider, plan = make_engine((original,))
    _, _, old = publish(engine, plan)
    corrected = make_bar(
        close="12",
        revision_id="r2",
        available_at="2025-01-04T00:00:00Z",
        ingested_at="2025-01-04T00:01:00Z",
    )
    provider.records = (original, corrected)
    candidate = engine.sync(plan)
    if operation == "snapshot":
        engine.publish(candidate)
    before = engine.publications(tail=True).cursor
    ctx = multiprocessing.get_context("spawn")
    channel, other = ctx.Pipe()
    process = ctx.Process(
        target=_publish_before_commit,
        args=(engine.store.root, candidate.model_dump_json(), operation, other),
    )
    with engine.open_reader(old.snapshot_id) as active:
        process.start()
        other.close()
        try:
            assert channel.poll(20)
            identity = channel.recv()
            directory = "dataset_versions" if operation == "version" else "snapshots"
            assert engine.store.path(f"{directory}/{identity}/manifest.json").is_file()
            lookup = (
                engine.store.dataset_version if operation == "version" else engine.store.snapshot
            )
            with pytest.raises(TiveloError) as uncommitted:
                lookup(identity)
            assert uncommitted.value.code == ErrorCode.SNAPSHOT_NOT_FOUND
            assert not engine.publications(after=before).items
            assert active.bars(query("2025-01-05T00:00:00Z")) == (original,)
            process.kill()
            process.join(10)
            assert process.exitcode is not None
            with pytest.raises(TiveloError) as rolled_back:
                lookup(identity)
            assert rolled_back.value.code == ErrorCode.SNAPSHOT_NOT_FOUND
            assert not engine.publications(after=before).items
            version = engine.publish(candidate)
            new = engine.create_snapshot((version.version_id,))
            assert identity == (version.version_id if operation == "version" else new.snapshot_id)
            with engine.open_reader(new.snapshot_id) as updated:
                assert updated.bars(query("2025-01-05T00:00:00Z")) == (corrected,)
            events = engine.publications(after=before).items
            assert [event.version_id for event in events] == (
                [candidate.version.version_id] if operation == "version" else []
            )
            assert active.bars(query("2025-01-05T00:00:00Z")) == (original,)
        finally:
            channel.close()
            if process.is_alive():
                process.kill()
            process.join(10)
            process.close()


def test_d08_index_capacity_error_rolls_back_then_reuses_the_staged_candidate(
    make_bar, make_engine, monkeypatch
):
    original = make_bar()
    engine, provider, plan = make_engine((original,))
    _, _, old = publish(engine, plan)
    corrected = make_bar(
        close="12",
        revision_id="r2",
        available_at="2025-01-04T00:00:00Z",
        ingested_at="2025-01-04T00:01:00Z",
    )
    provider.records = (original, corrected)
    candidate = engine.sync(plan)
    connection = engine.store.connection

    @contextmanager
    def limited_database():
        with connection() as db:
            # Exercise SQLite's real SQLITE_FULL rollback without filling the host volume.
            (pages,) = db.execute("PRAGMA page_count").fetchone()
            db.execute(f"PRAGMA max_page_count={pages}")
            db.execute(
                "CREATE TEMP TRIGGER consume_space AFTER INSERT ON main.versions BEGIN INSERT INTO pressure VALUES (zeroblob(1048576)); END"
            )
            yield db

    with connection() as db:
        db.execute("CREATE TABLE pressure(payload BLOB)")
    with engine.open_reader(old.snapshot_id) as active:
        with monkeypatch.context() as patches:
            patches.setattr(engine.store, "connection", limited_database)
            with pytest.raises(TiveloError) as full:
                engine.publish(candidate)
        assert full.value.code == ErrorCode.PUBLICATION_FAILED
        assert full.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_FULL
        with connection() as db:
            assert db.execute("SELECT count(*) FROM pressure").fetchone() == (0,)
            assert db.execute("SELECT count(*) FROM versions").fetchone() == (1,)
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert active.bars(query("2025-01-05T00:00:00Z")) == (original,)
        version = engine.publish(candidate)
        new = engine.create_snapshot((version.version_id,))
        with engine.open_reader(new.snapshot_id) as updated:
            assert updated.bars(query("2025-01-05T00:00:00Z")) == (corrected,)


def test_d08_concurrent_publish_and_reference_protection(make_bar, make_engine):
    engine, _, plan = make_engine((make_bar(),))
    candidate = engine.sync(plan)
    with ThreadPoolExecutor(max_workers=3) as executor:
        versions = list(executor.map(lambda _: engine.publish(candidate), range(3)))
    assert len({v.version_id for v in versions}) == 1
    snapshot = engine.create_snapshot((versions[0].version_id,))
    with engine.open_reader(snapshot.snapshot_id):
        with pytest.raises(TiveloError) as raised:
            engine.store.delete_snapshot(snapshot.snapshot_id)
        assert raised.value.code == ErrorCode.REFERENCED_ARTIFACT
    engine.store.pin(snapshot.snapshot_id, "task/durable")
    reopened = SnapshotStore(engine.store.root)
    with pytest.raises(TiveloError):
        reopened.delete_snapshot(snapshot.snapshot_id)
    reopened.release(snapshot.snapshot_id, "task/durable")
    reopened.delete_snapshot(snapshot.snapshot_id)
    assert reopened.dataset_version(versions[0].version_id) == versions[0]



@pytest.mark.parametrize("existing", [b"candidate", b"different"])
def test_concurrent_immutable_manifest_preserves_the_first_writer(tmp_path, monkeypatch, existing):
    store = SnapshotStore(tmp_path / "store")
    destination = store.path("example/manifest.json")

    def install_first(source, target):
        assert source.read_bytes() == b"candidate"
        assert target == destination
        target.write_bytes(existing)
        raise FileExistsError(target)

    monkeypatch.setattr(type(destination), "rename", install_first)
    if existing == b"candidate":
        store.write_atomic("example/manifest.json", b"candidate")
    else:
        with pytest.raises(TiveloError) as raised:
            store.write_atomic("example/manifest.json", b"candidate")
        assert raised.value.code == ErrorCode.SNAPSHOT_CORRUPT
    assert destination.read_bytes() == existing
    assert list(destination.parent.iterdir()) == [destination]

def test_killed_reader_releases_only_its_own_snapshot_reference(make_bar, make_engine):
    engine, _, plan = make_engine((make_bar(),))
    _, version, snap = publish(engine, plan)
    snapshot_id = snap.snapshot_id
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_reader_process, args=(engine.store.root, snapshot_id, child))
    process.start()
    child.close()
    try:
        assert parent.poll(20) and parent.recv() == snapshot_id
        with engine.open_reader(snapshot_id):
            with pytest.raises(TiveloError) as denied:
                engine.store.delete_snapshot(snapshot_id)
            assert denied.value.code == ErrorCode.REFERENCED_ARTIFACT
            other, _, other_plan = make_engine((make_bar(close="10", revision_id="r2"),))
            _, _, other_snapshot = publish(other, other_plan)
            assert other_snapshot.snapshot_id != snapshot_id
            other.store.delete_snapshot(other_snapshot.snapshot_id)
            process.kill()
            process.join(10)
            assert process.exitcode is not None
            with pytest.raises(TiveloError) as still_reading:
                engine.store.delete_snapshot(snapshot_id)
            assert still_reading.value.code == ErrorCode.REFERENCED_ARTIFACT
        # A pre-existing durable pin cannot be classified as abandoned by its name.
        legacy_owner = "reader/" + "a" * 32
        engine.store.pin(snapshot_id, legacy_owner)
        with pytest.raises(TiveloError) as persistent:
            engine.store.delete_snapshot(snapshot_id)
        assert persistent.value.code == ErrorCode.REFERENCED_ARTIFACT
        engine.store.release(snapshot_id, legacy_owner)
        reopened = SnapshotStore(engine.store.root)
        reopened.delete_snapshot(snapshot_id)
        assert reopened.dataset_version(version.version_id) == version
        with reopened.connection() as db:
            for table in ("refs", "reader_refs"):
                assert (
                    db.execute(
                        f"SELECT count(*) FROM {table} WHERE snapshot_id=?", (snapshot_id,)
                    ).fetchone()[0]
                    == 0
                )
    finally:
        parent.close()
        if process.is_alive():
            process.kill()
        process.join(10)
        process.close()


def test_d08_corrupt_facts_are_rejected_before_queries(make_bar, make_engine):
    engine, _, plan = make_engine((make_bar(),))
    _, version, snapshot = publish(engine, plan)
    file = engine.store.path(version.contents.artifacts[0].path)
    file.write_bytes(b"corrupt")
    with engine.open_reader(snapshot.snapshot_id) as reader, pytest.raises(TiveloError) as raised:
        reader.bars(query())
    assert raised.value.code == ErrorCode.SNAPSHOT_CORRUPT


def test_d05_partial_empty_and_failure_remain_distinct(make_bar, make_engine):
    partial = Coverage(
        status="partial",
        requested_units=2,
        completed_units=1,
        row_count=1,
        issues=(
            {"instrument_id": "security-2", "reason": "missing", "detail": "Unavailable partition"},
        ),
    )
    engine, provider, plan = make_engine((make_bar(),), coverage=partial)
    candidate = engine.sync(plan)
    assert candidate.version.contents.coverage.status == "partial"
    provider.records = ()
    provider.coverage = Coverage(
        status="empty",
        requested_units=1,
        completed_units=1,
        row_count=0,
        issues=({"reason": "suspended", "detail": "No trades"},),
    )
    candidate = engine.sync(plan)
    version = engine.publish(candidate)
    assert version.contents.coverage.status == "empty"
    snapshot = engine.create_snapshot((version.version_id,))
    with engine.open_reader(snapshot.snapshot_id) as reader:
        assert reader.bars(query()) == ()
    provider.coverage = Coverage(
        status="error",
        requested_units=1,
        completed_units=0,
        row_count=0,
        issues=({"reason": "source_error", "detail": "Source failed"},),
    )
    with pytest.raises(TiveloError) as raised:
        engine.sync(plan)
    assert raised.value.code == ErrorCode.SOURCE_UNAVAILABLE


def test_cancel_removes_only_its_staging_and_closes_source(make_bar, make_engine):
    engine, provider, plan = make_engine(
        (make_bar(), make_bar(instrument_id="security-2")), page_size=1
    )
    local = local_context()
    context = SyncContext(local.resolve_credentials, lambda: provider.calls >= 1, local.progress)
    with pytest.raises(TiveloError) as raised:
        engine.sync(plan, context)
    assert raised.value.code == ErrorCode.CANCELLED
    assert provider.closed == 1
    assert not list((engine.store.root / "staging").iterdir())


def test_d09_source_cannot_relabel_adjusted_prices_as_raw(make_bar, make_engine):
    engine, provider, plan = make_engine((make_bar(),))
    provider.records = (make_bar(price_basis="qfq"),)
    with pytest.raises(TiveloError) as raised:
        engine.sync(plan)
    assert raised.value.code == ErrorCode.CONTRACT_INVALID


def test_ambiguous_or_duplicate_revisions_are_not_published(make_bar, make_engine):
    for records in (
        (make_bar(), make_bar()),
        (make_bar(), make_bar(close="12", revision_id="other")),
    ):
        engine, _, plan = make_engine(records)
        with pytest.raises(TiveloError) as raised:
            engine.sync(plan)
        assert raised.value.code == ErrorCode.DATA_CONFLICT


def test_columnar_batches_are_bounded_and_do_not_construct_per_bar_models(
    make_bar, make_engine, monkeypatch
):
    from tivelo_contracts.data import Bar

    records = (
        make_bar(),
        make_bar(instrument_id="security-2"),
        make_bar(instrument_id="security-3"),
    )
    engine, _, plan = make_engine(records)
    _, _, snapshot = publish(engine, plan)
    request = BarQuery(
        instrument_ids=tuple(r.instrument_id for r in records),
        timeframe="1d",
        start=query().start,
        end=query().end,
        as_of=query().as_of,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("bulk reads must not instantiate a Bar per row")

    with engine.open_reader(snapshot.snapshot_id) as reader:
        assert isinstance(reader, ColumnarDataReader)
        monkeypatch.setattr(Bar, "model_validate", forbidden)
        batches = list(reader.iter_bar_batches(request, batch_size=2))
        assert [len(b) for b in batches] == [2, 1]
        table = pa.Table.from_batches([pa.record_batch(batch) for batch in batches])
        assert table["close"].to_pylist() == [Decimal("11")] * 3
        assert table.schema.field("close").type == pa.decimal128(38, 12)
        assert not list((engine.store.root / "staging").glob("reader-*"))


def test_early_bulk_reader_close_releases_query_resources(make_bar, make_engine):
    records = (make_bar(), make_bar(instrument_id="security-2"))
    engine, _, plan = make_engine(records)
    _, _, snapshot = publish(engine, plan)
    request = BarQuery(
        instrument_ids=tuple(r.instrument_id for r in records),
        timeframe="1d",
        start=query().start,
        end=query().end,
        as_of=query().as_of,
    )
    with engine.open_reader(snapshot.snapshot_id) as reader:
        batches = reader.iter_bar_batches(request, batch_size=1)
        assert len(next(batches)) == 1
        batches.close()
        assert not list((engine.store.root / "staging").glob("reader-*"))


def test_reader_context_releases_paused_bulk_query_resources(make_bar, make_engine):
    engine, _, plan = make_engine((make_bar(),))
    _, _, snapshot = publish(engine, plan)
    with engine.open_reader(snapshot.snapshot_id) as reader:
        batches = reader.iter_bar_batches(query(), batch_size=1)
        assert len(next(batches)) == 1
        assert list((engine.store.root / "staging").glob("reader-*"))
    assert not list((engine.store.root / "staging").glob("reader-*"))
    batches.close()


def test_pinned_reader_queries_within_budget_when_spill_directory_is_full(make_bar, make_engine, monkeypatch):
    import tivelo_data.reader as reader_module
    from tivelo_data import DataEngine

    row = make_bar()
    engine, _, plan = make_engine((row,))
    _, _, snapshot = publish(engine, plan)
    small_budget = DataEngine(engine.store.root, memory_limit="64KB")
    with engine.open_reader(snapshot.snapshot_id) as reader, small_budget.open_reader(snapshot.snapshot_id) as limited:
        def full(*args, **kwargs):
            raise OSError(errno.ENOSPC, "test filesystem is full")

        with monkeypatch.context() as patch:
            patch.setattr(reader_module.tempfile, "TemporaryDirectory", full)
            assert reader.bars(query()) == (row,)
            with pytest.raises(TiveloError) as stopped:
                limited.bars(query())
            assert stopped.value.code == ErrorCode.RESOURCE_BUSY
        assert reader.bars(query()) == (row,)


def test_large_fact_copy_does_not_hold_publication_index_lock(make_bar, make_engine, monkeypatch):
    import tivelo_data.storage as storage

    engine, provider, plan = make_engine((make_bar(),))
    _, _, snapshot = publish(engine, plan)
    provider.records = (make_bar(close="12"),)
    candidate = engine.sync(plan)
    entered, resume = Event(), Event()
    original_copy = storage.shutil.copyfileobj

    def slow_copy(source, destination):
        entered.set()
        assert resume.wait(timeout=5)
        return original_copy(source, destination)

    monkeypatch.setattr(storage.shutil, "copyfileobj", slow_copy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(engine.publish, candidate)
        try:
            assert entered.wait(timeout=5)
            # An existing snapshot can be pinned while a new fact file copies.
            pinning = pool.submit(engine.store.pin, snapshot.snapshot_id, "task/other")
            assert pinning.result(timeout=1) == snapshot
        finally:
            resume.set()
            publishing.result(timeout=5)
    engine.store.release(snapshot.snapshot_id, "task/other")
