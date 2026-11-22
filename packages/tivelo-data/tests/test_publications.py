import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_data import DataEngine
from tivelo_data.cli import main


def test_only_committed_distinct_versions_are_delivered_in_order(make_engine, make_bar):
    engine, source, plan = make_engine([make_bar()])
    candidate = engine.sync(plan)
    start = engine.publications(tail=True).cursor
    assert start.sequence == 0 and start.event_id is None
    assert not engine.publications(after=start).items
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: engine.publish(candidate), range(3)))
    assert all(v == candidate.version for v in results)
    first = engine.publications(after=start)
    assert [v.version_id for v in first.items] == [candidate.version.version_id]
    assert first.items[0].dataset_id == "bar" and first.items[0].sequence == first.cursor.sequence
    assert first.cursor.event_id == first.items[0].event_id and not first.has_more
    assert engine.create_snapshot((candidate.version.version_id,)).snapshot_id
    assert not engine.publications(after=first.cursor).items
    source.records = (make_bar(close="12"),)
    revised = engine.publish(engine.sync(plan))
    restored = DataEngine(engine.store.root)
    next_page = restored.publications(after=first.cursor)
    assert [v.version_id for v in next_page.items] == [revised.version_id]
    assert next_page.items[0].recorded_at.tzinfo is not None
    assert next_page.cursor.sequence > first.cursor.sequence
    assert restored.publications(tail=True).cursor == next_page.cursor


def test_filtered_pages_and_cli_checkpoints_resume_without_skipping(
    make_engine, make_bar, tmp_path, capsys
):
    engine, provider, plan = make_engine([make_bar()])
    other = engine.plan_sync(plan.request.model_copy(update={"dataset_id": "other"}))
    versions = []
    for index, actual in enumerate((plan, other, plan, other)):
        provider.records = (make_bar(volume=str(1000 + index)),)
        versions.append(engine.publish(engine.sync(actual)).version_id)
    head = engine.publications(tail=True)
    first = engine.publications(dataset_id="bar", limit=1)
    assert first.has_more and [v.version_id for v in first.items] == [versions[0]]
    second = engine.publications(after=first.cursor, limit=1)
    assert not second.has_more and [v.version_id for v in second.items] == [versions[2]]
    assert second.cursor.sequence == head.cursor.sequence and second.cursor.dataset_id == "bar"
    empty = engine.publications(dataset_id="absent")
    assert not empty.items and empty.cursor.sequence == head.cursor.sequence
    checkpoint = tmp_path / "cursor.json"
    checkpoint.write_text(first.cursor.model_dump_json())
    cmd = ["--root", str(engine.store.root), "publications"]
    assert main([*cmd, "--after", str(checkpoint), "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out) == second.model_dump(mode="json")
    for args, code in (
        (["--after", str(checkpoint), "--dataset", "other"], "DATA_CONFLICT"),
        (["--limit", "0"], "CONTRACT_INVALID"),
    ):
        assert main([*cmd, *args]) == 1
        assert json.loads(capsys.readouterr().err)["code"] == code


def test_cursor_rejects_another_store_a_rollback_and_a_divergent_copy(
    make_engine, make_bar, tmp_path
):
    engine, source, plan = make_engine([make_bar()])
    one = engine.publish(engine.sync(plan))
    checkpoint = engine.publications(tail=True).cursor
    duplicate = tmp_path / "fork"
    shutil.copytree(engine.store.root, duplicate)
    source.records = (make_bar(close="12"),)
    revised = engine.publish(engine.sync(plan))
    later = engine.publications(after=checkpoint).cursor
    fork, _, fork_plan = make_engine([make_bar(close="12")], directory=duplicate)
    for root in (DataEngine(tmp_path / "unrelated"), fork):
        with pytest.raises(TiveloError) as exc:
            root.publications(after=later)
        assert exc.value.code == ErrorCode.DATA_CONFLICT
    assert fork.publish(fork.sync(fork_plan)).version_id == revised.version_id != one.version_id
    on_fork = fork.publications(after=checkpoint)
    assert on_fork.cursor.sequence == later.sequence and on_fork.cursor.event_id != later.event_id
    with pytest.raises(TiveloError) as diverged:
        fork.publications(after=later)
    assert diverged.value.code == ErrorCode.DATA_CONFLICT


def test_failed_journal_write_rolls_back_version_and_retry_creates_one_receipt(
    make_engine, make_bar
):
    engine, _, plan = make_engine([make_bar()])
    candidate = engine.sync(plan)
    before = engine.publications(tail=True).cursor
    with engine.store.transaction() as db:
        db.execute(
            "CREATE TRIGGER reject_test_receipt BEFORE INSERT ON publication_events BEGIN "
            "SELECT RAISE(ABORT, 'test journal unavailable'); END"
        )
    with pytest.raises(TiveloError) as interrupted:
        engine.publish(candidate)
    assert interrupted.value.code == ErrorCode.PUBLICATION_FAILED
    assert engine.publications(after=before).cursor == before
    with pytest.raises(TiveloError) as unpublished:
        engine.store.dataset_version(candidate.version.version_id)
    assert unpublished.value.code == ErrorCode.SNAPSHOT_NOT_FOUND
    with engine.store.transaction() as db:
        db.execute("DROP TRIGGER reject_test_receipt")
    assert engine.publish(candidate) == engine.publish(candidate)
    (event,) = engine.publications(after=before).items
    assert event.version_id == candidate.version.version_id


@pytest.mark.parametrize(
    "damage",
    [
        "DELETE FROM metadata WHERE key='publication_stream'",
        "DROP TABLE publication_events",
    ],
)
def test_reopening_damaged_journal_reports_corruption(make_engine, make_bar, damage):
    engine, _, plan = make_engine([make_bar()])
    engine.publish(engine.sync(plan))
    with sqlite3.connect(engine.store.index) as db:
        db.execute(damage)
    if damage.startswith("DELETE"):
        with pytest.raises(TiveloError) as running_reader:
            engine.publications()
        assert running_reader.value.code == ErrorCode.SNAPSHOT_CORRUPT
    with pytest.raises(TiveloError) as corrupt:
        DataEngine(engine.store.root).publications()
    assert corrupt.value.code == ErrorCode.SNAPSHOT_CORRUPT
