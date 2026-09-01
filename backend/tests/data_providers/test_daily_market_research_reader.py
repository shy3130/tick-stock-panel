import json
import shutil
from datetime import date

import duckdb

from app.data_providers.fquant.daily_market_research import PublishedDailyMarketFactsReader


def _reader(tmp_path):
    db = tmp_path / "markets.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute(
        "CREATE TABLE daily_markets (code VARCHAR, asset_type INTEGER, trade_date DATE, price DOUBLE, Ztj DOUBLE, Jrkpj DOUBLE, Zgj DOUBLE, Zdj DOUBLE, Zrspj DOUBLE, name VARCHAR)"
    )
    conn.execute(
        "INSERT INTO daily_markets VALUES ('000001', 1, '2024-01-02', 10.4, 11, 10, 11, 9, 9.5, 'ABC')"
    )
    conn.execute(
        "INSERT INTO daily_markets VALUES ('000001', 1, '2024-01-03', 10.5, 11, 10, 11, 9, NULL, 'ABC')"
    )
    conn.execute(
        "INSERT INTO daily_markets VALUES "
        "('000002', 1, '2024-01-02', 9.8, 10.5, 10, 10.5, 9.5, 10, '*ST ABC')"
    )
    conn.execute(
        "INSERT INTO daily_markets VALUES ('000003', 1, '2024-01-02', 9.8, 11, 10, 11, 9, 10, NULL)"
    )
    conn.close()
    manifest = json.dumps(
        {"generation": "g1", "entries": [{"logical": "markets", "file": db.name}]}
    ).encode()
    return PublishedDailyMarketFactsReader(str(db), "g1", manifest)


def test_reader_preserves_direct_case_and_drops_incomplete_fact(tmp_path):
    reader = _reader(tmp_path)
    try:
        facts = reader.limit_band_facts("000001.SZ", date(2024, 1, 2), date(2024, 1, 3))
        assert set(facts) == {date(2024, 1, 2)}
        fact = facts[date(2024, 1, 2)]
        assert fact.raw_open == 10
        assert fact.raw_close == 10.4
        assert fact.pre_close == 9.5
        assert fact.published_limit_up == 11
        assert fact.published_limit_down == 8.55
    finally:
        reader.close()


def test_st_name_overrides_base_regime_for_lower_limit(tmp_path):
    reader = _reader(tmp_path)
    try:
        daily = reader.limit_band_facts("000002.SZ", date(2024, 1, 2), date(2024, 1, 2))[
            date(2024, 1, 2)
        ]
        batch = reader.escape_risk_facts(("000002.SZ",), date(2024, 1, 2))["000002.SZ"][0]
        for fact in (daily, batch):
            assert fact.regime == "st_5"
            assert fact.is_st is True
            assert fact.published_limit_down == 9.5
            assert fact.signal_limit_down is True
    finally:
        reader.close()


def test_missing_pit_name_censors_limit_regime_facts(tmp_path):
    reader = _reader(tmp_path)
    try:
        assert reader.limit_band_facts("000003.SZ", date(2024, 1, 2), date(2024, 1, 2)) == {}
        assert reader.escape_risk_facts(("000003.SZ",), date(2024, 1, 2)) == {}
    finally:
        reader.close()


def test_turnover_fields_missing_fail_closed_without_breaking_limit_facts(tmp_path):
    reader = _reader(tmp_path)
    try:
        assert reader.daily_turnover_fact("000001.SZ", date(2024, 1, 2)) is None
        assert reader.intraday_float_shares_fact("000001.SZ", date(2024, 1, 2)) is None
        facts = reader.escape_risk_facts(("000001.SZ",), date(2024, 1, 2))
        assert facts["000001.SZ"][0].published_limit_up == 11
        assert facts["000001.SZ"][1] is None
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
    reader = PublishedDailyMarketFactsReader.from_canonical_manifest(
        {"source_generations": {"markets": "g1"}}
    )
    try:
        assert reader.pin_identity_verified() is False
        assert reader.pin_verification_mode() == "missing_expected_hash"
    finally:
        reader.close()


def test_turnover_facts_separate_close_hslv_from_lagged_intraday_shares(tmp_path):
    db = tmp_path / "markets-with-turnover.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute(
        "CREATE TABLE daily_markets (code VARCHAR, asset_type INTEGER, "
        "trade_date DATE, price DOUBLE, ztj DOUBLE, jrkpj DOUBLE, zgj DOUBLE, "
        "zdj DOUBLE, zrspj DOUBLE, name VARCHAR, ltgb DOUBLE, hslv DOUBLE)"
    )
    conn.execute(
        "INSERT INTO daily_markets VALUES "
        "('000001', 1, '2026-08-05', 10.1, 11, 10, 11, 9, 9.4, 'ABC', 900000, 0.47), "
        "('000001', 1, '2026-08-06', 10.4, 11, 10, 11, 9, 9.5, 'ABC', 1000000, 3.2), "
        "('000001', 1, '2026-08-07', 10.6, 11, 10, 11, 9, 9.6, 'ABC', 1100000, NULL)"
    )
    conn.close()
    manifest = json.dumps(
        {"generation": "g2", "entries": [{"logical": "markets", "file": db.name}]}
    ).encode()
    reader = PublishedDailyMarketFactsReader(str(db), "g2", manifest)
    try:
        daily = reader.daily_turnover_fact("000001.SZ", date(2026, 8, 6))
        assert daily is not None
        assert daily.reported_turnover_pct == 3.2
        assert daily.source_day == date(2026, 8, 6)
        assert daily.available_at.isoformat() == "2026-08-06T15:00:00+08:00"
        assert daily.availability_basis == "daily_market_close"

        intraday = reader.intraday_float_shares_fact("000001.SZ", date(2026, 8, 6))
        assert intraday is not None
        assert intraday.float_shares == 900_000
        assert intraday.source_day == date(2026, 8, 5)
        assert intraday.available_at.isoformat() == "2026-08-05T15:00:00+08:00"
        assert intraday.availability_basis == "previous_daily_market_close"

        batch = reader.escape_risk_facts(("000001.SZ",), date(2026, 8, 6))
        assert batch["000001.SZ"][1] == intraday

        assert reader.daily_turnover_fact("000001.SZ", date(2026, 8, 7)) is None
        null_hslv_fallback = reader.intraday_float_shares_fact(
            "000001.SZ", date(2026, 8, 7)
        )
        assert null_hslv_fallback is not None
        assert null_hslv_fallback.float_shares == 1_000_000
        assert null_hslv_fallback.source_day == date(2026, 8, 6)
    finally:
        reader.close()
