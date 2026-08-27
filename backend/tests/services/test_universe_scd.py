from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import json
import duckdb
import pytest

from app.services.universe_scd import (
    CollectionDraft,
    PublishedUniverseScdReader,
    UniverseScdConflict,
    UniverseScdIntegrityError,
    UniverseScdNoCoverage,
    UniverseScdNotPublished,
    _manifest_digest_core,
    _parse_intervals,
    canonical_json_bytes,
    collect_eligible_universe,
    publish_collection,
    sha256_hex,
    validate_root_outside_data_dir,
)


SOURCE = {
    "artifact": "fstore_snapshot",
    "root_env": "FQUANT_SNAPSHOT_ROOT_FSTORE",
    "root": "/tmp/fstore",
    "logical": "fstore",
    "generation": "20260821T153000",
    "manifest_sha256": "a" * 64,
    "file": "fstore.duckdb",
    "size_bytes": 1,
}


def _write_fstore(tmp_path, *, generation="20260821T153000", extra=False):
    root = tmp_path / "fstore"
    gen = root / generation
    gen.mkdir(parents=True)
    db_path = gen / "fstore.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE base_infos(code VARCHAR, ssdate DATE, asset_type INTEGER)")
        conn.execute("INSERT INTO base_infos VALUES ('600000', '2020-01-01', 1)")
        if extra:
            conn.execute("INSERT INTO base_infos VALUES ('600001', '2020-01-01', 1)")
        conn.execute("CREATE TABLE trade_date(tdate DATE, isopen INTEGER, mkt INTEGER, lastdate DATE, nextdate DATE)")
        start = date(2026, 8, 14)
        for offset in range(20):
            day = start + timedelta(days=offset)
            isopen = 3 if day.weekday() < 5 else 1
            conn.execute("INSERT INTO trade_date VALUES (?, ?, 1, NULL, NULL)", [day, isopen])
    finally:
        conn.close()
    manifest = {
        "generation": generation,
        "entries": [{"logical": "fstore", "file": "fstore.duckdb", "size_bytes": db_path.stat().st_size}],
    }
    (gen / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    (root / "current.json").write_bytes(canonical_json_bytes({"generation": generation}))
    return root


def test_collector_uses_exact_fstore_trade_date_and_base_infos_connection(tmp_path, monkeypatch):
    root = _write_fstore(tmp_path)
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_FSTORE", str(root))
    draft = collect_eligible_universe(now=datetime(2026, 8, 21, 8, tzinfo=timezone.utc))
    assert draft.effective_from == date(2026, 8, 24)
    assert draft.prev_market_day == date(2026, 8, 21)
    assert draft.symbols == ("600000.SH",)
    assert draft.calendar_identity.startswith("fstore_trade_date:20260821T153000:")

def test_collector_rejects_non_integer_or_negative_fstore_size(tmp_path, monkeypatch):
    root = _write_fstore(tmp_path)
    manifest_path = root / "20260821T153000" / "manifest.json"
    manifest = {"generation": "20260821T153000", "entries": [{"logical": "fstore", "file": "fstore.duckdb", "size_bytes": True}]}
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_FSTORE", str(root))
    with pytest.raises(UniverseScdIntegrityError):
        collect_eligible_universe(now=datetime(2026, 8, 21, 8, tzinfo=timezone.utc))


def _draft(symbols=("600000.SH",), *, effective=date(2026, 8, 24), available="2026-08-21T08:00:00+00:00"):
    content_hash = sha256_hex(canonical_json_bytes(list(symbols)))
    return CollectionDraft(
        available_at=available,
        collection_date=date(2026, 8, 21),
        effective_from=effective,
        prev_market_day=date(2026, 8, 21),
        calendar_identity="calendar:test",
        calendar_contract="fstore_trade_date:tdate,isopen,mkt,lastdate,nextdate",
        source=dict(SOURCE),
        symbols=tuple(symbols),
        content_hash=content_hash,
    )


def test_reader_rejects_newest_interval_not_pointing_to_current_generation(tmp_path):
    publish_collection(tmp_path, tmp_path / "data", _draft())
    current = json.loads((tmp_path / "current.json").read_text())
    generation = current["generation"]
    manifest_path = tmp_path / generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["intervals"][-1]["source_generation"] = "20260821T080000Z-aaaaaaaaaaaaaaaa"
    core = dict(manifest)
    core.pop("generation")
    assert generation.rsplit("-", 1)[-1] == sha256_hex(canonical_json_bytes(_manifest_digest_core(core)))[:16]
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(UniverseScdIntegrityError, match="newest interval"):
        PublishedUniverseScdReader(tmp_path, data_dir=tmp_path / "data")

def test_reader_before_first_real_collection_is_observable_unavailable(tmp_path):
    with pytest.raises(UniverseScdNotPublished):
        PublishedUniverseScdReader(tmp_path)


def test_first_collection_starts_on_next_market_day_and_has_no_prior_coverage(tmp_path):
    publish_collection(tmp_path, tmp_path / "data", _draft())
    reader = PublishedUniverseScdReader(tmp_path, data_dir=tmp_path / "data")
    with pytest.raises(UniverseScdNoCoverage):
        reader.snapshot_identity(date(2026, 8, 21))
    identity = reader.snapshot_identity(date(2026, 8, 24))
    assert identity["effective_from"] == date(2026, 8, 24)
    assert reader.eligible_symbols(date(2026, 8, 24)) == ["600000.SH"]


def test_second_collection_closes_parent_interval_and_changes_symbols(tmp_path):
    publish_collection(tmp_path, tmp_path / "data", _draft())
    second = _draft(
        symbols=("600001.SH",),
        effective=date(2026, 8, 25),
        available="2026-08-24T08:00:00+00:00",
    )
    second = replace(second, collection_date=date(2026, 8, 24), prev_market_day=date(2026, 8, 24))
    publish_collection(tmp_path, tmp_path / "data", second)
    reader = PublishedUniverseScdReader(tmp_path, data_dir=tmp_path / "data")
    assert reader.eligible_symbols(date(2026, 8, 24)) == ["600000.SH"]
    assert reader.eligible_symbols(date(2026, 8, 25)) == ["600001.SH"]


def test_same_effective_day_identical_source_and_content_is_idempotent(tmp_path):
    first = publish_collection(tmp_path, tmp_path / "data", _draft())
    second = publish_collection(tmp_path, tmp_path / "data", _draft())
    assert first.status == "published"
    assert second.status == "idempotent"
    assert second.generation == first.generation


def test_same_effective_day_different_content_is_conflict(tmp_path):
    publish_collection(tmp_path, tmp_path / "data", _draft())
    with pytest.raises(UniverseScdConflict):
        publish_collection(tmp_path, tmp_path / "data", _draft(symbols=("600001.SH",)))



def test_parent_current_cas_conflict_leaves_pointer_unchanged(tmp_path, monkeypatch):
    module = __import__("app.services.universe_scd", fromlist=["_read_current"])
    publish_collection(tmp_path, tmp_path / "data", _draft())
    original = module._read_current
    first_current = original(str(tmp_path))
    calls = {"count": 0}

    def changed_current(root):
        calls["count"] += 1
        return first_current if calls["count"] == 1 else b'{"generation":"tampered"}'

    monkeypatch.setattr(module, "_read_current", changed_current)
    next_draft = replace(
        _draft(symbols=("600001.SH",), effective=date(2026, 8, 25), available="2026-08-24T08:00:00+00:00"),
        collection_date=date(2026, 8, 24),
        prev_market_day=date(2026, 8, 24),
    )
    outcome = publish_collection(tmp_path, tmp_path / "data", next_draft)
    assert outcome.status == "conflict"
    assert original(str(tmp_path)) == first_current
def test_root_equal_or_inside_data_dir_is_rejected(tmp_path):
    with pytest.raises(UniverseScdIntegrityError):
        validate_root_outside_data_dir(tmp_path / "data", tmp_path / "data")
    with pytest.raises(UniverseScdIntegrityError):
        validate_root_outside_data_dir(tmp_path / "data" / "scd", tmp_path / "data")
    validate_root_outside_data_dir(tmp_path / "scd", tmp_path / "data")


def test_interval_integrity_rejects_duplicate_or_overlapping_ranges():
    base = {
        "content_hash": "a" * 64,
        "available_at": "2026-08-21T08:00:00+00:00",
        "source_generation": "20260821T080000Z-aaaaaaaaaaaaaaaa",
    }
    with pytest.raises(UniverseScdIntegrityError):
        _parse_intervals([
            {**base, "effective_from": "2026-08-24", "effective_to": "2026-08-26"},
            {**base, "effective_from": "2026-08-26", "effective_to": None},
        ])


def test_reader_prefetch_returns_identity_and_frozen_symbols_for_each_day(tmp_path):
    publish_collection(tmp_path, tmp_path / "data", _draft())
    reader = PublishedUniverseScdReader(tmp_path, data_dir=tmp_path / "data")
    prefetched = reader.prefetch_event_days([date(2026, 8, 24)])
    assert prefetched[date(2026, 8, 24)][1] == ["600000.SH"]
    assert prefetched[date(2026, 8, 24)][0]["content_hash"] == _draft().content_hash
