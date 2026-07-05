from datetime import datetime

from app.data_providers.fquant_provider import FQuantProvider


class FakeFundEngine:
    def __init__(self):
        self.calls = []

    def get_fund_daily(self, code, date_iso):
        assert date_iso == "2026-07-01"
        self.calls.append(code)
        if code != "600519.SH":
            return {}
        return {
            "main_net": 300.0,
            "total_net": 0.0,
            "super_large_net": 100.0,
            "large_net": 200.0,
            "medium_net": -50.0,
            "small_net": -250.0,
        }


class FailingMoneyflow:
    def get_daily(self, codes, date_iso):  # noqa: ARG002
        raise AssertionError("disk moneyflow should not call HTTP fallback")


def test_moneyflow_daily_prefers_disk_fund_source():
    engine = FakeFundEngine()
    provider = object.__new__(FQuantProvider)
    provider._engine = engine
    provider._engine_mode = "disk"
    provider._moneyflow = FailingMoneyflow()
    provider.name = "fquant_local"

    df = provider.get_moneyflow_daily(["600519.SH"], datetime(2026, 7, 1))

    row = df.to_dicts()[0]
    assert engine.calls == ["600519.SH"]
    assert row["symbol"] == "600519.SH"
    assert row["source"] == "fquant_local:moneyflow:daily"
    assert row["main_net"] == 300.0
    assert row["total_net"] == 0.0
    assert row["super_large_net"] == 100.0
    assert row["large_net"] == 200.0


class PartialMoneyflow:
    def __init__(self):
        self.calls = []

    def get_daily(self, codes, date_iso):
        self.calls.append((codes, date_iso))
        return {"300059": {"main_net": 99.0, "total_net": 88.0}}


def test_moneyflow_daily_falls_back_for_disk_misses_only():
    engine = FakeFundEngine()
    provider = object.__new__(FQuantProvider)
    provider._engine = engine
    provider._engine_mode = "disk"
    provider._moneyflow = PartialMoneyflow()
    provider.name = "fquant_local"

    df = provider.get_moneyflow_daily(["600519.SH", "300059.SZ"], datetime(2026, 7, 1))

    rows = {r["symbol"]: r for r in df.to_dicts()}
    assert engine.calls == ["600519.SH", "300059.SZ"]
    assert provider._moneyflow.calls == [(["300059"], "2026-07-01")]
    assert rows["600519.SH"]["main_net"] == 300.0
    assert rows["300059.SZ"]["main_net"] == 99.0


class FakeFundRangeEngine:
    def __init__(self, df):
        self._df = df
        self.calls = []

    def get_fund_range(self, code, start_iso, end_iso, asset_type=None):
        self.calls.append((code, start_iso, end_iso, asset_type))
        return self._df


class EngineWithoutFundRange:
    """模拟 HTTP 模式的 engine —— 没有 get_fund_range 方法。"""


def test_moneyflow_range_forwards_to_disk_engine():
    import polars as pl

    fake_df = pl.DataFrame({"date": ["2026-07-01"], "main_net_inflow": [300.0]})
    engine = FakeFundRangeEngine(fake_df)
    provider = object.__new__(FQuantProvider)
    provider._engine = engine
    provider._engine_mode = "disk"
    provider.name = "fquant_local"

    df = provider.get_moneyflow_range("600519.SH", datetime(2026, 6, 30), datetime(2026, 7, 1))

    assert engine.calls == [("600519.SH", "2026-06-30", "2026-07-01", None)]
    assert df.to_dicts() == [{"date": "2026-07-01", "main_net_inflow": 300.0}]


def test_moneyflow_range_returns_empty_for_http_engine():
    import polars as pl

    provider = object.__new__(FQuantProvider)
    provider._engine = EngineWithoutFundRange()
    provider._engine_mode = "http"
    provider.name = "fquant"

    df = provider.get_moneyflow_range("600519.SH", datetime(2026, 6, 30), datetime(2026, 7, 1))

    assert isinstance(df, pl.DataFrame)
    assert df.is_empty()
