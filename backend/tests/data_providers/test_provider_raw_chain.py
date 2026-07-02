from datetime import datetime

from app.data_providers.fquant_provider import FQuantProvider


class FakeEngine:
    def __init__(self):
        self.keys = []

    def get_wide(self, code, limit=250):
        self.keys.append(("wide", code))
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

    def get_xdxr(self, code):
        self.keys.append(("xdxr", code))
        return [{"date": "2024-06-19", "category": 1, "fenhong": 30}]


class FakeEngineWithPreClose:
    def get_wide(self, code, limit=250):
        return [
            {"date": "2024-06-19", "close": 90.0},
            {"date": "2024-06-18", "close": 100.0},
        ]

    def get_xdxr(self, code):
        return []


class FakeFStore:
    def query(self, sql, params=None):
        if "t_1_day_klines" not in sql:
            return []
        return [{
            "date": "2012-10-26",
            "oracle_open": 248.72,
            "oracle_high": 248.98,
            "oracle_low": 240.07,
            "oracle_close": 241.0,
        }]


class FakeIndexEngine:
    def get_wide(self, code, limit=250):  # noqa: ARG002
        return [{
            "date": "2026-07-01",
            "open": 4090.76,
            "high": 4143.31,
            "low": 4087.54,
            "close": 4112.45,
            "volume": 672_331_800,
            "amount": 1_698_487_599_104,
        }]

    def get_xdxr(self, code):  # noqa: ARG002
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


def test_disk_engine_uses_symbol_key_to_preserve_exchange():
    engine = FakeEngine()
    provider = object.__new__(FQuantProvider)
    provider._engine = engine
    provider._fstore = FakeFStore()
    provider._engine_mode = "disk"
    provider.name = "fquant_local"

    provider._get_daily_from_engine_wide("000001.SH", "000001", None, None)

    assert engine.keys == [("wide", "000001.SH"), ("xdxr", "000001.SH")]


def test_index_daily_does_not_use_stock_raw_oracle_for_same_code():
    provider = object.__new__(FQuantProvider)
    provider._engine = FakeIndexEngine()
    provider._fstore = FakeFStore()
    provider._engine_mode = "disk"
    provider.name = "fquant_local"

    rows = provider._get_daily_from_engine_wide("000001.SH", "000001", None, None, "index")

    assert rows[0]["close"] == 4112.45
