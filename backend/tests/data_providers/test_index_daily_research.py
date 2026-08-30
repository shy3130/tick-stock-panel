import hashlib
import json
import shutil
from datetime import date

import duckdb
import pytest

from app.data_providers.fquant.index_daily_research import (
    REASON_COVERAGE_INSUFFICIENT,
    REASON_SOURCE_CONFLICT,
    IndexDailyReadRequest,
    PublishedIndexDailyReader,
)


def _snapshots(tmp_path, *, conflict=False):
    root = tmp_path / "snapshots"
    gen = root / "g1"
    gen.mkdir(parents=True)
    kp, mp = tmp_path / "klines.duckdb", tmp_path / "markets.duckdb"
    kc = duckdb.connect(str(kp))
    kc.execute(
        "CREATE TABLE day_klines (code VARCHAR, asset_type INTEGER, ktype INTEGER, fq INTEGER, tdate VARCHAR, close DOUBLE, cjl DOUBLE)"
    )
    kc.executemany(
        "INSERT INTO day_klines VALUES (?,10,101,0,?,?,?)",
        [
            ("000001", "2024-01-02", 100.0, 10),
            ("000001", "2024-01-03", 101.0, 10),
            ("000001", "2024-01-04", 102.0, 10),
            ("000001", "2024-01-05", 103.0, 10),
            ("000001", "2024-01-06", 999.0, 10),
        ],
    )
    kc.execute("INSERT INTO day_klines VALUES ('000001',10,5,0,'2024-01-07',500,1)")
    kc.close()
    mc = duckdb.connect(str(mp))
    mc.execute(
        "CREATE TABLE daily_markets (code VARCHAR, asset_type INTEGER, trade_date VARCHAR, price DOUBLE, payload_json VARCHAR)"
    )
    overlap = 999.0 if conflict else 102.0
    mc.executemany(
        "INSERT INTO daily_markets VALUES (?,10,?,?,?)",
        [
            ("000001", "2024-01-04", overlap, '{"Cjl":"12"}'),
            ("000001", "2024-01-05", 103.0, '{"Cjl":"12"}'),
            ("000001", "2024-01-08", 104.0, '{"Cjl":"12"}'),
        ],
    )
    mc.close()
    shutil.copy2(kp, gen / "klines.duckdb")
    shutil.copy2(mp, gen / "markets.duckdb")
    manifest = {
        "generation": "g1",
        "entries": [
            {"logical": "klines", "file": "klines.duckdb"},
            {"logical": "markets", "file": "markets.duckdb"},
        ],
    }
    raw = json.dumps(manifest).encode()
    (gen / "manifest.json").write_bytes(raw)
    h = hashlib.sha256(raw).hexdigest()
    return root, {
        "source_generations": {
            "klines": {"generation": "g1", "manifest_sha256": h},
            "markets": {"generation": "g1", "manifest_sha256": h},
        }
    }


def test_pinned_tail_merge_and_coverage(tmp_path, monkeypatch):
    root, canonical = _snapshots(tmp_path)
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_FSTORE", str(root))
    reader = PublishedIndexDailyReader.from_canonical_manifest(canonical)
    try:
        panel = reader.read_index_daily(
            IndexDailyReadRequest(codes=["000001"], start=date(2024, 1, 1), end=date(2024, 1, 10))
        )
        leg = panel.legs[0]
        assert leg.status == "ok"
        assert [x.date for x in leg.bars] == [
            date(2024, 1, 2),
            date(2024, 1, 3),
            date(2024, 1, 4),
            date(2024, 1, 5),
            date(2024, 1, 6),
            date(2024, 1, 8),
        ]
        assert leg.coverage.markets_tail_rows_merged == 1
        assert leg.coverage.markets_overlap_rows_checked == 2
        assert panel.pin.pin_verified is True
    finally:
        reader.close()


def test_overlap_mismatch_is_fail_closed(tmp_path, monkeypatch):
    root, canonical = _snapshots(tmp_path, conflict=True)
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_FSTORE", str(root))
    reader = PublishedIndexDailyReader.from_canonical_manifest(canonical)
    try:
        leg = reader.read_index_daily(
            {"codes": ["000001"], "start": date(2024, 1, 1), "end": date(2024, 1, 10)}
        ).legs[0]
        assert leg.status == "unavailable" and leg.reason_code == REASON_SOURCE_CONFLICT
        assert leg.coverage.close_conflict_rows == 1
    finally:
        reader.close()


def test_missing_klines_base_is_coverage_unavailable(tmp_path, monkeypatch):
    root, canonical = _snapshots(tmp_path)
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_FSTORE", str(root))
    reader = PublishedIndexDailyReader.from_canonical_manifest(canonical)
    try:
        leg = reader.read_index_daily(
            {"codes": ["000300"], "start": date(2024, 1, 1), "end": date(2024, 1, 10)}
        ).legs[0]
        assert leg.status == "unavailable" and leg.reason_code == REASON_COVERAGE_INSUFFICIENT
    finally:
        reader.close()


def test_pin_hash_mismatch_fails_closed(tmp_path, monkeypatch):
    root, canonical = _snapshots(tmp_path)
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_FSTORE", str(root))
    canonical["source_generations"]["markets"]["manifest_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="identity mismatch"):
        PublishedIndexDailyReader.from_canonical_manifest(canonical)
