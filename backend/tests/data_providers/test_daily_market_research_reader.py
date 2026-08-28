from datetime import date
import json
import shutil

import duckdb

from app.data_providers.fquant.daily_market_research import PublishedDailyMarketFactsReader


def _reader(tmp_path):
    db = tmp_path / "markets.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE daily_markets (code VARCHAR, asset_type INTEGER, trade_date DATE, price DOUBLE, Ztj DOUBLE, Jrkpj DOUBLE, Zgj DOUBLE, Zdj DOUBLE, Zrspj DOUBLE, name VARCHAR)")
    conn.execute("INSERT INTO daily_markets VALUES ('000001', 1, '2024-01-02', 10.4, 11, 10, 11, 9, 9.5, 'ABC')")
    conn.execute("INSERT INTO daily_markets VALUES ('000001', 1, '2024-01-03', 10.5, 11, 10, 11, 9, NULL, 'ABC')")
    conn.close()
    manifest = json.dumps({'generation': 'g1', 'entries': [{'logical': 'markets', 'file': db.name}]}).encode()
    return PublishedDailyMarketFactsReader(str(db), 'g1', manifest)


def test_reader_preserves_direct_case_and_drops_incomplete_fact(tmp_path):
    reader = _reader(tmp_path)
    try:
        facts = reader.limit_band_facts('000001.SZ', date(2024, 1, 2), date(2024, 1, 3))
        assert set(facts) == {date(2024, 1, 2)}
        fact = facts[date(2024, 1, 2)]
        assert fact.raw_open == 10
        assert fact.raw_close == 10.4
        assert fact.pre_close == 9.5
        assert fact.published_limit_up == 11
        assert fact.published_limit_down == 8.55
    finally:
        reader.close()



def test_canonical_generation_manifest_identity_without_expected_hash(tmp_path, monkeypatch):
    direct = _reader(tmp_path)
    direct.close()
    root = tmp_path / "root" / "g1"
    root.mkdir(parents=True)
    shutil.copy2(tmp_path / "markets.duckdb", root / "markets.duckdb")
    manifest = {"generation": "g1", "entries": [{"logical": "markets", "file": "markets.duckdb"}]}
    (root / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setenv("FQUANT_SNAPSHOT_ROOT_FSTORE", str(tmp_path / "root"))
    reader = PublishedDailyMarketFactsReader.from_canonical_manifest({"source_generations": {"markets": "g1"}})
    try:
        assert reader.pin_identity_verified() is False
        assert reader.pin_verification_mode() == "missing_expected_hash"
    finally:
        reader.close()