import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest

from app.services.universe_presence_history import (
    PresenceHistoryIntegrityError,
    PresenceHistoryNoCoverageError,
    PresenceHistoryNotMarketDayError,
    PresenceStatus,
    PublishedPresenceUniverseReader,
    collect_presence_history,
    publish_presence_history,
)
from app.services.universe_scd import canonical_json_bytes, sha256_hex


def _source(tmp_path, rows=None, generation="20260821T153000"):
    root = tmp_path / "fstore"
    gd = root / generation
    gd.mkdir(parents=True)
    start = date(2026, 8, 17)
    days = [start + timedelta(days=i) for i in range(12)]
    fpath, mpath = gd / "fstore.duckdb", gd / "markets.duckdb"
    c = duckdb.connect(str(fpath))
    c.execute(
        "CREATE TABLE trade_date(tdate DATE,isopen INTEGER,mkt VARCHAR,lastdate DATE,nextdate DATE)"
    )
    for day in days:
        is_open = 3 if day.weekday() < 5 else 1
        c.execute("INSERT INTO trade_date VALUES (?, ?, 'A股', NULL, NULL)", [day, is_open])
        c.execute("INSERT INTO trade_date VALUES (?, ?, '港股', NULL, NULL)", [day, is_open])
    c.close()
    c = duckdb.connect(str(mpath))
    c.execute("CREATE TABLE daily_markets(code VARCHAR,trade_date DATE,asset_type INTEGER)")
    for code, d in rows or [("600000", d) for d in days if d.weekday() < 5]:
        c.execute("INSERT INTO daily_markets VALUES (?, ?, 1)", [code, d])
    c.close()
    manifest = {
        "generation": generation,
        "created_at": "2026-08-21T15:30:00Z",
        "entries": [
            {"logical": "fstore", "file": fpath.name, "size_bytes": fpath.stat().st_size},
            {"logical": "markets", "file": mpath.name, "size_bytes": mpath.stat().st_size},
        ],
    }
    (gd / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    (root / "current.json").write_bytes(canonical_json_bytes({"generation": generation}))
    return root


def _publish(tmp_path, rows=None):
    src = _source(tmp_path, rows)
    out = tmp_path / "presence"
    result = publish_presence_history(
        out, tmp_path / "data", source_root=src, now=datetime(2026, 8, 29, tzinfo=UTC)
    )
    return src, out, result


def _rewrite_manifest(root, generation, mutate):
    generation_dir = root / generation
    manifest = json.loads((generation_dir / "manifest.json").read_bytes())
    mutate(manifest)
    core = {
        key: value for key, value in manifest.items() if key not in ("generation", "published_at")
    }
    new_generation = (
        manifest["generation"].rsplit("-", 1)[0] + "-" + sha256_hex(canonical_json_bytes(core))[:16]
    )
    manifest["generation"] = new_generation
    new_generation_dir = root / new_generation
    generation_dir.rename(new_generation_dir)
    (new_generation_dir / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    (root / "current.json").write_bytes(
        canonical_json_bytes({"generation": new_generation}) + b"\n"
    )
    return new_generation


def test_source_pin_accepts_pretty_manifest_and_hashes_raw_bytes(tmp_path):
    root = _source(tmp_path)
    manifest_path = root / "20260821T153000" / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    pretty_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    manifest_path.write_bytes(pretty_bytes)

    draft = collect_presence_history(source_root=root)

    assert draft.source["manifest_sha256"] == sha256_hex(pretty_bytes)


def test_exact_day_presence_and_gapless_empty_day(tmp_path):
    _, root, result = _publish(
        tmp_path, [("600000", date(2026, 8, 17)), ("600000", date(2026, 8, 19))]
    )
    reader = PublishedPresenceUniverseReader(root)
    assert result.status == "published"
    assert reader.presence_status("600000.SH", date(2026, 8, 17)) is PresenceStatus.PRESENT
    assert reader.presence_status("600000.SH", date(2026, 8, 18)) is PresenceStatus.NOT_OBSERVED
    assert reader.snapshot(date(2026, 8, 18)).symbols == ()
    assert reader.snapshot(date(2026, 8, 18)).source_day_observed is False
    assert len(reader.source_manifest()["intervals"]) == 3


def test_weekend_and_coverage_fail_closed(tmp_path):
    _, root, _ = _publish(tmp_path, [("600000", date(2026, 8, 17)), ("600000", date(2026, 8, 24))])
    reader = PublishedPresenceUniverseReader(root)
    with pytest.raises(PresenceHistoryNoCoverageError):
        reader.snapshot(date(2026, 8, 16))
    with pytest.raises(PresenceHistoryNoCoverageError):
        reader.snapshot(date(2026, 8, 25))
    with pytest.raises(PresenceHistoryNotMarketDayError):
        reader.snapshot(date(2026, 8, 22))


def test_source_pin_rejects_duplicate_and_invalid_codes(tmp_path):
    with pytest.raises(PresenceHistoryIntegrityError):
        collect_presence_history(
            source_root=_source(
                tmp_path / "duplicate",
                [("600000", date(2026, 8, 17)), ("600000", date(2026, 8, 17))],
            )
        )
    with pytest.raises(PresenceHistoryIntegrityError):
        collect_presence_history(
            source_root=_source(tmp_path / "invalid", [("ABC123", date(2026, 8, 17))])
        )


def test_idempotent_same_source_core(tmp_path):
    src = _source(tmp_path)
    root = tmp_path / "presence"
    first = publish_presence_history(root, tmp_path / "data", source_root=src)
    second = publish_presence_history(root, tmp_path / "data", source_root=src)
    assert (
        first.status == "published"
        and second.status == "idempotent"
        and first.generation == second.generation
    )


def test_reader_rejects_path_tampering_after_valid_generation_rehash(tmp_path):
    _, root, result = _publish(tmp_path)

    _rewrite_manifest(
        root,
        result.generation,
        lambda manifest: manifest["intervals"][0].__setitem__("symbols_file", "../outside.json"),
    )

    with pytest.raises(PresenceHistoryIntegrityError, match="interval malformed"):
        PublishedPresenceUniverseReader(root)


def test_reader_rejects_gap_and_reused_hash_identity_tampering(tmp_path):
    rows = [
        ("600000", date(2026, 8, 17)),
        ("600000", date(2026, 8, 19)),
    ]
    _, gap_root, gap_result = _publish(tmp_path / "gap", rows)
    _rewrite_manifest(
        gap_root,
        gap_result.generation,
        lambda manifest: manifest["intervals"][1].__setitem__(
            "effective_from", manifest["intervals"][0]["effective_to"]
        ),
    )
    with pytest.raises(PresenceHistoryIntegrityError, match="interval gap/overlap"):
        PublishedPresenceUniverseReader(gap_root)

    _, identity_root, identity_result = _publish(tmp_path / "identity", rows)
    _rewrite_manifest(
        identity_root,
        identity_result.generation,
        lambda manifest: manifest["intervals"][2].__setitem__("symbol_count", 2),
    )
    with pytest.raises(PresenceHistoryIntegrityError, match="interval symbol identity mismatch"):
        PublishedPresenceUniverseReader(identity_root)


def test_reader_rejects_market_days_and_source_identity_tampering(tmp_path):
    _, days_root, days_result = _publish(tmp_path / "days")
    market_days_path = days_root / days_result.generation / "market_days.json"
    market_days_path.write_bytes(market_days_path.read_bytes() + b" ")
    with pytest.raises(PresenceHistoryIntegrityError, match="market days hash mismatch"):
        PublishedPresenceUniverseReader(days_root)

    _, source_root, source_result = _publish(tmp_path / "source")
    _rewrite_manifest(
        source_root,
        source_result.generation,
        lambda manifest: manifest["source"]["logicals"].pop("markets"),
    )
    with pytest.raises(PresenceHistoryIntegrityError, match="source logical identities malformed"):
        PublishedPresenceUniverseReader(source_root)


def test_reader_rejects_non_integer_schema_and_market_day_counts(tmp_path):
    only_day = [("600000", date(2026, 8, 17))]
    _, count_root, count_result = _publish(tmp_path / "count", only_day)

    def replace_counts(manifest):
        manifest["market_days"]["count"] = True
        manifest["coverage"]["market_day_count"] = 1.0

    _rewrite_manifest(count_root, count_result.generation, replace_counts)
    with pytest.raises(PresenceHistoryIntegrityError, match="market days/count/coverage mismatch"):
        PublishedPresenceUniverseReader(count_root)

    _, schema_root, schema_result = _publish(tmp_path / "schema", only_day)
    _rewrite_manifest(
        schema_root,
        schema_result.generation,
        lambda manifest: manifest.__setitem__("schema_version", 2.0),
    )
    with pytest.raises(PresenceHistoryIntegrityError, match="manifest contract mismatch"):
        PublishedPresenceUniverseReader(schema_root)


def test_reader_rejects_symlinked_symbol_artifact(tmp_path):
    _, root, result = _publish(tmp_path)
    generation_dir = root / result.generation
    manifest = json.loads((generation_dir / "manifest.json").read_bytes())
    symbols_path = generation_dir / manifest["intervals"][0]["symbols_file"]
    outside = tmp_path / "outside-symbols.json"
    outside.write_bytes(symbols_path.read_bytes())
    symbols_path.unlink()
    symbols_path.symlink_to(outside)

    with pytest.raises(OSError):
        PublishedPresenceUniverseReader(root)


def test_publish_current_cas_conflict_keeps_prior_generation(tmp_path, monkeypatch):
    first_source = _source(tmp_path / "first")
    root = tmp_path / "presence"
    data_dir = tmp_path / "data"
    first = publish_presence_history(root, data_dir, source_root=first_source)
    second_source = _source(
        tmp_path / "second",
        generation="20260821T160000",
    )

    from app.services import universe_presence_history as presence_module

    real_stage = presence_module._stage

    def race_current(root_path, generation, manifest_bytes, payloads):
        staging = real_stage(root_path, generation, manifest_bytes, payloads)
        current_path = root / "current.json"
        current_path.write_bytes(current_path.read_bytes() + b"\n")
        return staging

    monkeypatch.setattr(presence_module, "_stage", race_current)
    result = publish_presence_history(root, data_dir, source_root=second_source)

    assert result.status == "conflict"
    assert json.loads((root / "current.json").read_bytes())["generation"] == first.generation
    assert not (root / result.generation).exists()
    assert PublishedPresenceUniverseReader(root).source_manifest()["generation"] == first.generation


def test_invalid_draft_cannot_replace_current(tmp_path):
    source, root, result = _publish(tmp_path)
    draft = collect_presence_history(source_root=source)
    bad = replace(draft, day_hashes=("0" * 64, *draft.day_hashes[1:]))

    with pytest.raises(PresenceHistoryIntegrityError, match="draft symbol identity mismatch"):
        publish_presence_history(root, tmp_path / "data", draft=bad)

    assert json.loads((root / "current.json").read_bytes())["generation"] == result.generation


def test_presence_reader_has_no_eligible_interface_and_snapshots_are_frozen(tmp_path):
    _, root, _ = _publish(tmp_path)
    reader = PublishedPresenceUniverseReader(root)
    snap = reader.snapshot(date(2026, 8, 17))
    assert not hasattr(reader, "eligible_symbols") and not hasattr(reader, "prefetch_event_days")
    assert {item.value for item in PresenceStatus} == {"present", "not_observed"}
    with pytest.raises(FrozenInstanceError):
        snap.symbols = ()


def test_repository_property_is_separate(tmp_path, monkeypatch):
    _, root, _ = _publish(tmp_path)
    monkeypatch.setenv("TICKFLOW_UNIVERSE_PRESENCE_ROOT", str(root))
    from app.storage.repository import KlineRepository

    fake = type("Repo", (), {"store": type("Store", (), {"data_dir": tmp_path / "data"})()})()
    assert isinstance(
        KlineRepository.pit_presence_universe.fget(fake), PublishedPresenceUniverseReader
    )
