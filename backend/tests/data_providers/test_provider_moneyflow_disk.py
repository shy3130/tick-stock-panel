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
