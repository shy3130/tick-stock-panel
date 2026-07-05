import polars as pl
from datetime import date

from app.services import market_overview_builder as builder


class FakeProvider:
    def __init__(self):
        self.calls = []

    def get_latest_market_supplements(self, symbols):
        self.calls.append(list(symbols))
        return pl.DataFrame([
            {"symbol": "000001.SZ", "date": "2026-07-03", "turnover_rate": 0.44, "change_pct": 0.0268},
            {"symbol": "603986.SH", "date": "2026-07-03", "turnover_rate": 9.33, "change_pct": -0.0298},
        ])


def test_fill_turnover_from_provider_when_enriched_missing(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    rows = [
        {"symbol": "000001.SZ", "turnover_rate": None},
        {"symbol": "603986.SH", "turnover_rate": None},
    ]

    out = builder._fill_market_supplements_from_provider(rows, date(2026, 7, 3))

    assert provider.calls == [["000001.SZ", "603986.SH"]]
    assert [r["turnover_rate"] for r in out] == [0.44, 9.33]


def test_fill_market_supplements_replaces_existing_change_pct(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    rows = [{"symbol": "000001.SZ", "turnover_rate": 1.23, "change_pct": -0.09}]

    out = builder._fill_market_supplements_from_provider(rows, date(2026, 7, 3))

    assert provider.calls == [["000001.SZ"]]
    assert out[0]["turnover_rate"] == 0.44
    assert out[0]["change_pct"] == 0.0268


def test_fill_market_supplements_skips_stale_snapshot(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    rows = [{"symbol": "000001.SZ", "turnover_rate": 1.23, "change_pct": -0.09}]

    out = builder._fill_market_supplements_from_provider(rows, date(2026, 7, 4))

    assert out == rows
    assert provider.calls == [["000001.SZ"]]


def test_fill_market_supplements_replaces_implausible_change_pct(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    rows = [{"symbol": "000001.SZ", "turnover_rate": 1.23, "change_pct": -0.96}]

    out = builder._fill_market_supplements_from_provider(rows, date(2026, 7, 3))

    assert out[0]["turnover_rate"] == 0.44
    assert out[0]["change_pct"] == 0.0268
