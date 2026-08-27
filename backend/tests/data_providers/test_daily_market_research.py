from __future__ import annotations

import hashlib
import json
from datetime import date

import duckdb
import pytest

from app.data_providers.fquant import daily_market_research as module
from app.data_providers.fquant.daily_market_research import PublishedDailyMarketFactsReader


def _publish(tmp_path, monkeypatch):
    generation = "20260827T120000"
    root = tmp_path / "snapshots"
    gen = root / generation
    gen.mkdir(parents=True)
    db = gen / "markets.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute(
        "CREATE TABLE daily_markets (code VARCHAR, asset_type INTEGER, trade_date DATE, "
        "jrkpj DOUBLE, zgj DOUBLE, zdj DOUBLE, price DOUBLE, cjl DOUBLE, cje DOUBLE, "
        "ztj DOUBLE, name VARCHAR, payload_json VARCHAR)"
    )
    conn.execute(
        "INSERT INTO daily_markets VALUES "
        "('600519',1,'2026-08-26',100,110,95,105,1000,200000,115,'贵州茅台',NULL), "
        "('600519',1,'2026-08-27',105,120,100,118,2000,300000,130,'贵州茅台',NULL), "
        "('000001',1,'2026-08-27',10,11,9,0,1,2,12,'平安银行',NULL), "
        "('000002',1,'2026-08-27',4.8,5,4.7,4.9,100,5000,5.15,'*ST测试',NULL), "
        "('000003',1,'2026-08-27',4.8,5,4.7,4.9,100,5000,5.39,NULL,NULL), "
        "('920001',1,'2021-11-14',10,11,9,10,100,1000,10.5,'*ST北测',NULL), "
        "('920001',1,'2021-11-15',10,11,9,10,100,1000,10.5,'*ST北测',NULL), "
        "('600519',3,'2026-08-27',1,1,1,1,1,1,1,'贵州茅台',NULL)"
    )
    conn.execute(
        "INSERT INTO daily_markets VALUES "
        "('600519',1,'2022-03-04',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,?)",
        [json.dumps({"Price": 91.5, "Ztj": 100.65, "Name": "贵州茅台"})],
    )
    conn.close()
    manifest_bytes = json.dumps(
        {"generation": generation, "entries": [{"logical": "markets", "file": "markets.duckdb"}]}
    ).encode()
    (gen / "manifest.json").write_bytes(manifest_bytes)
    monkeypatch.setattr(module, "current_path", lambda logical: str(db))
    return db, generation, hashlib.sha256(manifest_bytes).hexdigest()


def test_missing_snapshot_fails_closed(monkeypatch):
    monkeypatch.setattr(module, "current_path", lambda logical: None)
    with pytest.raises(FileNotFoundError):
        PublishedDailyMarketFactsReader.from_repository(object())


def test_reader_pins_generation_and_maps_fields(tmp_path, monkeypatch):
    db, generation, manifest_hash = _publish(tmp_path, monkeypatch)
    reader = PublishedDailyMarketFactsReader.from_repository(object())
    assert reader.generation() == generation
    assert reader.manifest_sha256() == manifest_hash
    assert reader.market_days(date(2026, 8, 26), date(2026, 8, 27)) == [
        date(2026, 8, 26),
        date(2026, 8, 27),
    ]
    assert reader.universe(date(2026, 8, 27), date(2026, 8, 27)) == [
        "000002.SZ",
        "000003.SZ",
        "600519.SH",
    ]
    assert reader.limit_regime_facts("600519.SH", date(2026, 8, 26), date(2026, 8, 27)) == {
        date(2026, 8, 26): {
            "limit_up_price": 115.0,
            "name": "贵州茅台",
            "is_st": False,
            "regime": "main_10",
        },
        date(2026, 8, 27): {
            "limit_up_price": 130.0,
            "name": "贵州茅台",
            "is_st": False,
            "regime": "main_10",
        },
    }
    assert reader.limit_regime_facts(
        "000002.SZ",
        date(2026, 8, 27),
        date(2026, 8, 27),
    ) == {
        date(2026, 8, 27): {
            "limit_up_price": 5.15,
            "name": "*ST测试",
            "is_st": True,
            "regime": "st_5",
        }
    }
    assert (
        reader.limit_regime_facts(
            "000003.SZ",
            date(2026, 8, 27),
            date(2026, 8, 27),
        )
        == {}
    )
    assert reader.limit_regime_facts(
        "600519.SH",
        date(2022, 3, 4),
        date(2022, 3, 4),
    ) == {
        date(2022, 3, 4): {
            "limit_up_price": 100.65,
            "name": "贵州茅台",
            "is_st": False,
            "regime": "main_10",
        }
    }
    assert reader.limit_regime_facts(
        "920001.BJ",
        date(2021, 11, 14),
        date(2021, 11, 15),
    ) == {
        date(2021, 11, 15): {
            "limit_up_price": 10.5,
            "name": "*ST北测",
            "is_st": True,
            "regime": "st_5",
        }
    }
    monkeypatch.setattr(module, "current_path", lambda logical: str(db.parent / "other.duckdb"))
    assert reader.generation() == generation
    reader.close()
    with pytest.raises(RuntimeError):
        reader.market_days(date(2026, 8, 26), date(2026, 8, 27))


def test_board_regime_is_date_aware():
    regime = PublishedDailyMarketFactsReader._regime
    assert regime("300001.SZ", date(2020, 8, 23)) == "main_10"
    assert regime("300001.SZ", date(2020, 8, 24)) == "chinext_20"
    assert regime("688001.SH", date(2019, 7, 21)) is None
    assert regime("688001.SH", date(2019, 7, 22)) == "star_20"
    assert regime("920001.BJ", date(2021, 11, 14)) is None
    assert regime("920001.BJ", date(2021, 11, 15)) == "beijing_30"
