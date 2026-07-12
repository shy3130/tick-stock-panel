from datetime import datetime

import pytest

from app.data_providers.fquant_provider import FQuantProvider


class FakeEngine:
    def __init__(self):
        self.keys = []

    def get_wide(self, code, limit=250, asset_type=None):  # noqa: ARG002
        self.keys.append(("wide", code, asset_type))
        return [{
            "date": "2012-10-26",
            "open": -105.170712,
            "high": -104.955836,
            "low": -112.319473,
            "close": -111.476498,
            "volume": 2_971_600,
            "amount": -326_710_000,
            "last_close": -107.360795,
            "change_rate": -1.2,
        }]

    def get_xdxr(self, code, asset_type=None):  # noqa: ARG002
        self.keys.append(("xdxr", code, asset_type))
        return [{"date": "2024-06-19", "category": 1, "fenhong": 30}]


class FakeEngineWithPreClose:
    def get_wide(self, code, limit=250, asset_type=None):  # noqa: ARG002
        return [
            {"date": "2024-06-19", "close": 90.0},
            {"date": "2024-06-18", "close": 100.0},
        ]

    def get_xdxr(self, code, asset_type=None):  # noqa: ARG002
        return []


class FakeFStore:
    def query(self, sql, params=None):
        if "t_1_daily_markets" in sql:
            return []
        if "t_1_day_klines" not in sql:
            return []
        return [{
            "date": "2012-10-26",
            "oracle_open": 248.72,
            "oracle_high": 248.98,
            "oracle_low": 240.07,
            "oracle_close": 241.0,
        }]


class FakeFStoreWithDailyMarkets:
    def query(self, sql, params=None):  # noqa: ARG002
        if "t_1_day_klines" in sql:
            return [{
                "date": "2026-07-01",
                "oracle_open": 50.0,
                "oracle_high": 55.0,
                "oracle_low": 47.0,
                "oracle_close": 53.09,
            }]
        if "daily_markets" in sql:
            return [{
                "date": "2026-07-01",
                "oracle_open": 34.25,
                "oracle_high": 39.74,
                "oracle_low": 33.80,
                "oracle_close": 37.85,
                "oracle_volume": 12_300,
                "oracle_amount": 456,
            }]
        return []


class RecordingFStore:
    def __init__(self):
        self.sql = []

    def query(self, sql, params=None):  # noqa: ARG002
        self.sql.append(sql)
        if "t_20_day_klines" not in sql:
            return []
        return [{
            "tdate": "2026-07-02",
            "open": 1.0,
            "high": 1.02,
            "low": 0.99,
            "close": 1.01,
            "cjl": 1000,
            "cje": 1010,
            "zf": 1.0,
        }]


class FakeIndexEngine:
    def get_wide(self, code, limit=250, asset_type=None):  # noqa: ARG002
        return [{
            "date": "2026-07-01",
            "open": 4090.76,
            "high": 4143.31,
            "low": 4087.54,
            "close": 4112.45,
            "volume": 672_331_800,
            "amount": 1_698_487_599_104,
        }]

    def get_xdxr(self, code, asset_type=None):  # noqa: ARG002
        raise AssertionError("index daily must not use stock xdxr/raw oracle")


def test_engine_wide_uses_raw_oracle_before_daily_mapping():
    provider = object.__new__(FQuantProvider)
    provider._engine = FakeEngine()
    provider._fstore = FakeFStore()
    provider.name = "fquant"

    rows = provider._get_daily_from_engine_wide("600519.SH", "600519", None, None)

    assert rows[0]["open"] == 248.72
    assert rows[0]["high"] == 248.98
    assert rows[0]["low"] == 240.07
    assert rows[0]["close"] == 241.0


def test_daily_close_map_uses_raw_close():
    provider = object.__new__(FQuantProvider)
    provider._engine = FakeEngine()
    provider._fstore = FakeFStore()
    provider.name = "fquant"

    closes = provider._build_daily_close_map("600519.SH", "600519", None, None)

    assert closes["2012-10-26"] == 241.0


def test_raw_oracle_prefers_daily_markets_over_day_klines():
    provider = object.__new__(FQuantProvider)
    provider._fstore = FakeFStoreWithDailyMarkets()

    rows = provider._get_raw_oracle_rows("300492", [{"date": "2026-07-01"}])

    assert rows == [{
        "date": "2026-07-01",
        "oracle_open": 34.25,
        "oracle_high": 39.74,
        "oracle_low": 33.80,
        "oracle_close": 37.85,
        "oracle_volume": 12_300,
        "oracle_amount": 456,
    }]


def test_daily_close_map_keeps_pre_close_before_requested_start():
    provider = object.__new__(FQuantProvider)
    provider._engine = FakeEngineWithPreClose()
    provider._fstore = FakeFStore()
    provider.name = "fquant"

    closes = provider._build_daily_close_map(
        "600519.SH",
        "600519",
        datetime(2024, 6, 19),
        datetime(2024, 6, 19),
    )

    assert closes["2024-06-18"] == 100.0


def test_disk_engine_uses_code_key():
    engine = FakeEngine()
    provider = object.__new__(FQuantProvider)
    provider._engine = engine
    provider._fstore = FakeFStore()
    provider.name = "fquant"

    provider._get_daily_from_engine_wide("000001.SH", "000001", None, None)

    assert engine.keys == [("wide", "000001", "stock"), ("xdxr", "000001", "stock")]


def test_hk_daily_uses_code_key_and_skips_stock_raw_reconstruction():
    engine = FakeEngine()
    provider = object.__new__(FQuantProvider)
    provider._engine = engine
    provider._fstore = FakeFStore()
    provider.name = "fquant"

    rows = provider._get_daily_from_engine_wide("02577.HK", "02577", None, None, "hk")

    assert rows[0]["symbol"] == "02577.HK"
    assert engine.keys == [("wide", "02577", "hk")]


def test_index_daily_does_not_use_stock_raw_oracle_for_same_code():
    provider = object.__new__(FQuantProvider)
    provider._engine = FakeIndexEngine()
    provider._fstore = FakeFStore()
    provider.name = "fquant"

    rows = provider._get_daily_from_engine_wide("000001.SH", "000001", None, None, "index")

    assert rows[0]["close"] == 4112.45


def test_etf_fstore_daily_uses_asset_type_20_table():
    fstore = RecordingFStore()
    provider = object.__new__(FQuantProvider)
    provider._fstore = fstore
    provider.name = "fquant"

    rows = provider._get_daily_from_fstore_klines(
        "513050.SH",
        "513050",
        datetime(2026, 7, 1),
        datetime(2026, 7, 3),
        "etf",
    )

    assert rows[0]["symbol"] == "513050.SH"
    assert rows[0]["close"] == 1.01
    assert rows[0]["volume"] == 100_000
    assert "t_20_day_klines" in fstore.sql[0]
    assert all("day_klines " not in sql for sql in fstore.sql[1:])


class RecordingFStoreHK:
    """cjl=14_963_400 / real volume=1_496.0 是 hk00700 2025-10-20 的真实核对值
    (与 tdx-hk.duckdb market_day_kline.volume 逐日比对, 比值稳定在 ~10000)。"""

    def __init__(self):
        self.sql = []

    def query(self, sql, params=None):  # noqa: ARG002
        self.sql.append(sql)
        if "t_3_day_klines" not in sql:
            return []
        return [{
            "tdate": "2025-10-20",
            "open": 625.0,
            "high": 630.0,
            "low": 620.0,
            "close": 627.5,
            "cjl": 14_963_400,
            "cje": 9_384_000_000,
            "zf": 1.0,
        }]


def test_hk_fstore_daily_uses_asset_type_3_table_and_correct_volume_multiplier():
    """回归测试: klines_rows_to_daily 之前对所有市场统一用 cjl*100, 港股正确
    倍数是 cjl/10000 (少了 100万倍); 表选择之前对非 etf 一律查 t_1_day_klines
    (A股表), 港股永远查不到数据。"""
    fstore = RecordingFStoreHK()
    provider = object.__new__(FQuantProvider)
    provider._fstore = fstore
    provider.name = "fquant"

    rows = provider._get_daily_from_fstore_klines(
        "00700.HK",
        "00700",
        datetime(2025, 10, 20),
        datetime(2025, 10, 20),
        "hk",
    )

    assert "t_3_day_klines" in fstore.sql[0]
    assert rows[0]["symbol"] == "00700.HK"
    assert rows[0]["close"] == 627.5
    assert rows[0]["volume"] == pytest.approx(1_496.34, rel=1e-4)
