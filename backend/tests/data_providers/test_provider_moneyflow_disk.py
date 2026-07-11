from datetime import datetime
import polars as pl
from app.data_providers.fquant_provider import FQuantProvider


class FakeFundEngine:
    def __init__(self):
        self.calls = []

    def get_fund_daily(self, code, date_iso):
        assert date_iso == "2026-07-01"
        self.calls.append(code)
        if code != "600519":
            return {}
        return {
            "main_net": 300.0,
            "total_net": 0.0,
            "super_large_net": 100.0,
            "large_net": 200.0,
            "medium_net": -50.0,
            "small_net": -250.0,
        }


def test_moneyflow_daily_calls_engine_fund():
    engine = FakeFundEngine()
    provider = object.__new__(FQuantProvider)
    provider._engine = engine
    provider.name = "fquant"

    df = provider.get_moneyflow_daily(["600519.SH"], datetime(2026, 7, 1))

    row = df.to_dicts()[0]
    assert engine.calls == ["600519"]
    assert row["symbol"] == "600519.SH"
    assert row["source"] == "fquant:moneyflow:daily"
    assert row["main_net"] == 300.0
    assert row["total_net"] == 0.0
    assert row["super_large_net"] == 100.0
    assert row["large_net"] == 200.0


class FakeFundRangeEngine:
    def __init__(self, df):
        self._df = df
        self.calls = []

    def get_fund_range(self, code, start_iso, end_iso):
        self.calls.append((code, start_iso, end_iso))
        return self._df


def test_moneyflow_range_calls_engine_fund_range():
    fake_df = pl.DataFrame({"date": ["2026-07-01"], "main_net_inflow": [300.0]})
    engine = FakeFundRangeEngine(fake_df)
    provider = object.__new__(FQuantProvider)
    provider._engine = engine
    provider.name = "fquant"

    df = provider.get_moneyflow_range("600519.SH", datetime(2026, 6, 30), datetime(2026, 7, 1))

    assert engine.calls == [("600519", "2026-06-30", "2026-07-01")]
    assert df.to_dicts() == [{"date": "2026-07-01", "main_net_inflow": 300.0}]
