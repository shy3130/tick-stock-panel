from datetime import datetime
import os
from pathlib import Path

import math

import pytest

from app.data_providers.fquant.engine_data_disk import EngineDataDiskClient, _csv_path, _tdx_name
from app.data_providers.fquant_provider import FQuantProvider


DAY = """date,open,close,high,low,volume,amount
2026-07-01,10,11,12,9,1000000,11000000
"""
HK = """date,open,close,high,low,volume,amount
2026-07-01,5,5.5,6,4.8,10000,0
"""
MINUTES = """Price,Vol
11.1,100
"""
TRANS = """time,price,vol,num,amount,buyorsell
09:30,11.1,100,1,1110,1
"""


def test_disk_daily_units_and_wide_fallback(tmp_path, monkeypatch):
    root = tmp_path / "day" / "sz000"
    root.mkdir(parents=True)
    (root / "sz000001.csv").write_text(DAY)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    rows = EngineDataDiskClient().get_wide("000001", limit=10)

    assert rows[0]["date"] == "2026-07-01"
    assert rows[0]["close"] > 0
    assert rows[0]["volume"] >= 0
    assert rows[0]["amount"] >= 0
    assert 1 <= rows[0]["amount"] / max(rows[0]["volume"], 1) <= 1000


def test_hk_zero_amount_does_not_crash(tmp_path, monkeypatch):
    market, name, group_len = _tdx_name("02577.HK")
    assert (market, name) == ("hk", "hk02577")
    root = tmp_path / "wide" / name[:group_len]
    root.mkdir(parents=True)
    (root / f"{name}.csv").write_text(HK)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    rows = EngineDataDiskClient().get_wide("02577", limit=10, asset_type="hk")

    assert rows[0]["amount"] == 0
    assert math.isfinite(rows[0]["close"])


def test_missing_minutes_and_trans_return_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))
    client = EngineDataDiskClient()
    assert client.get_minutes("600519", "20260701") == []
    assert client.get_trans("600519", "20260701") == []


def test_minutes_and_trans_basic_fields(tmp_path, monkeypatch):
    (tmp_path / "minutes" / "2026" / "20260701").mkdir(parents=True)
    (tmp_path / "trans" / "2026" / "20260701").mkdir(parents=True)
    (tmp_path / "minutes" / "2026" / "20260701" / "sh600519.csv").write_text(MINUTES)
    (tmp_path / "trans" / "2026" / "20260701" / "sh600519.csv").write_text(TRANS)
    monkeypatch.setenv("TDX_DATA_DIR", str(tmp_path))

    client = EngineDataDiskClient()

    assert client.get_minutes("600519", "20260701") == [{"price": 11.1, "volume": 10_000.0}]
    assert client.get_trans("600519", "20260701")[0] == {
        "time": "09:30",
        "price": 11.1,
        "volume": 100,
        "amount": 1110,
        "order_count": 1,
        "direction": 1,
    }


def test_raw_reconstruct_maotai_20121026_smoke(monkeypatch):
    base = Path("/Volumes/vol3/tdx")
    if not _csv_path(base, "day", "600519.SH").exists():
        pytest.skip("TDX disk not mounted")
    if not os.environ.get("FSTORE_DATABASE_PASSWORD"):
        pytest.skip("FSTORE_DATABASE_PASSWORD not set")
    monkeypatch.setenv("TDX_DATA_DIR", str(base))
    df = FQuantProvider(engine_mode="disk").get_daily(
        ["600519.SH"],
        datetime(2012, 10, 26),
        datetime(2012, 10, 26),
        "stock",
    )
    assert not df.is_empty()
    close = float(df["close"][0])
    assert abs(close - 241.0) < 0.01
