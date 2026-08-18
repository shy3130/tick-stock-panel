from datetime import date, datetime

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


class FakeIndexMarkets:
    def query(self, sql, params=None):
        assert "asset_type = 10" in sql
        assert params[0] == "000001"
        return [
            {
                "date": "2026-07-01",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
                "amount": 1.0,
            },
            {
                "date": "2026-07-02",
                "open": 4110.0,
                "high": 4125.0,
                "low": 4105.0,
                "close": 4120.0,
                "volume": 700_000_000,
                "amount": 1_800_000_000_000,
            },
        ]


def test_daily_freshness_uses_newest_source_in_get_daily_chain():
    provider = object.__new__(FQuantProvider)
    provider._engine = type(
        "Engine",
        (),
        {"freshness": lambda _self: date(2026, 8, 14)},
    )()

    class FStore:
        @staticmethod
        def query(sql, params=None):  # noqa: ARG004
            assert "FROM t_1_day_klines" in sql
            return [{"latest_date": "2026-08-17"}]

    provider._fstore = FStore()

    assert provider.get_daily_freshness() == date(2026, 8, 17)


def test_ranged_stock_daily_fills_newer_fstore_date_without_overwriting_engine():
    provider = object.__new__(FQuantProvider)
    provider.name = "fquant"
    provider._get_daily_from_engine_wide = lambda *_args: [
        {
            "symbol": "600519.SH",
            "date": "2026-08-14",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 10,
            "amount": 1000,
        },
    ]
    provider._get_daily_from_fstore_klines = lambda *_args: [
        {
            "symbol": "600519.SH",
            "date": "2026-08-14",
            "open": 99,
            "high": 100,
            "low": 98,
            "close": 99,
            "volume": 9,
            "amount": 900,
        },
        {
            "symbol": "600519.SH",
            "date": "2026-08-17",
            "open": 109,
            "high": 111,
            "low": 108,
            "close": 110,
            "volume": 11,
            "amount": 1210,
        },
    ]

    result = provider.get_daily(
        ["600519.SH"],
        datetime(2026, 8, 14),
        datetime(2026, 8, 17),
        "stock",
    )

    assert result["date"].to_list() == [date(2026, 8, 14), date(2026, 8, 17)]
    assert result["close"].to_list() == [100.0, 110.0]


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


def test_raw_oracle_prefers_day_klines_over_daily_markets():
    provider = object.__new__(FQuantProvider)
    provider._fstore = FakeFStoreWithDailyMarkets()

    rows = provider._get_raw_oracle_rows("300492", [{"date": "2026-07-01"}])

    assert rows == [{
        "date": "2026-07-01",
        "oracle_open": 50.0,
        "oracle_high": 55.0,
        "oracle_low": 47.0,
        "oracle_close": 53.09,
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


def test_index_daily_fills_newer_dates_from_daily_markets():
    provider = object.__new__(FQuantProvider)
    provider._engine = FakeIndexEngine()
    provider._fstore = FakeFStore()
    provider._fstore_markets = FakeIndexMarkets()
    provider.name = "fquant"

    result = provider.get_daily(
        ["000001.INDEX"],
        datetime(2026, 7, 1),
        datetime(2026, 7, 2),
        "index",
    )

    assert result["date"].to_list() == [date(2026, 7, 1), date(2026, 7, 2)]
    assert result["close"].to_list() == [4112.45, 4120.0]


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
    """00700 2025-10-20 的真实 fstore 行。物理约束 cje/close = 股数：
    9,379,380,224 / 627.5 = 14,947,219 ≈ cjl(14,963,400)，即港股 cjl 本身
    就是股数，倍数 ×1。"""

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
            "cje": 9_379_380_224.0,
            "zf": 1.0,
        }]


def test_hk_fstore_daily_uses_asset_type_3_table_and_correct_volume_multiplier():
    """回归测试，钉住两件事：

    1. 表选择：之前对非 etf 一律查 t_1_day_klines(A股表)，港股永远查不到数据。
    2. volume 倍数：港股 cjl 已经是股数(×1)。之前统一 ×100 会高估 100 倍；
       若改成 ÷10000 去对齐 tdx-hk 的 market_day_kline.volume 列，那一列是
       "手"不是"股"，会低估 1 万倍——两种都错。用 amount/close 这个不依赖
       任何 volume 列的物理约束来校验。
    """
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
    assert rows[0]["volume"] == 14_963_400

    implied_shares = rows[0]["amount"] / rows[0]["close"]
    assert rows[0]["volume"] == pytest.approx(implied_shares, rel=0.01)


def test_hk_adjustment_and_financial_boundaries_fail_closed():
    provider = object.__new__(FQuantProvider)

    assert provider.get_adj_factors(
        ["00700.HK"],
        datetime(2024, 1, 1),
        datetime(2024, 1, 31),
        "hk",
    ).is_empty()
    assert provider.get_financial("00700.HK", "income").is_empty()
    assert provider.get_corp_action("00700.HK").is_empty()


def test_market_data_status_exposes_hk_unavailable_reasons():
    provider = object.__new__(FQuantProvider)

    status = provider.get_market_data_status()

    assert status["hk_adjustment"]["available"] is False
    assert "no HK corporate-action" in status["hk_adjustment"]["reason"]
    assert status["hk_financial"]["available"] is False
    assert "no HK symbols" in status["hk_financial"]["reason"]
