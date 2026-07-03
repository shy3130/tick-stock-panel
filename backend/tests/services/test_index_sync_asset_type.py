from datetime import datetime

import polars as pl

from app.services import index_sync
from app.capabilities import Cap, CapabilityLimits, CapabilitySet


class FakeProvider:
    def __init__(self):
        self.calls = []

    def get_daily(self, symbols, start_time, end_time, asset_type):
        self.calls.append((tuple(symbols), asset_type))
        return pl.DataFrame({
            "symbol": symbols,
            "date": [start_time.date()] * len(symbols),
            "open": [1.0] * len(symbols),
            "high": [1.0] * len(symbols),
            "low": [1.0] * len(symbols),
            "close": [1.0] * len(symbols),
            "volume": [1.0] * len(symbols),
            "amount": [1.0] * len(symbols),
        })


class FakeRepo:
    def __init__(self):
        self.index_daily = []
        self.etf_daily = []

    def append_index_daily(self, df):
        self.index_daily.append(df)

    def append_index_enriched(self, df):
        pass

    def append_etf_daily(self, df):
        self.etf_daily.append(df)

    def append_etf_enriched(self, df):
        pass

    def refresh_index_views(self):
        pass


def capset():
    return CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(batch=500)})


def test_index_daily_sync_passes_index_asset_type(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.kline_sync._get_data_provider", lambda: provider)
    monkeypatch.setattr("app.services.index_sync.compute_enriched", lambda raw, **kwargs: raw)

    repo = FakeRepo()
    rows = index_sync.sync_and_persist_index_daily(
        repo,
        capset(),
        start_date=datetime(2026, 7, 1),
        end_date=datetime(2026, 7, 1),
        symbols_override=["000001.SH"],
    )

    assert rows == 1
    assert provider.calls == [(("000001.SH",), "index")]


def test_etf_daily_sync_passes_etf_asset_type(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.kline_sync._get_data_provider", lambda: provider)
    monkeypatch.setattr("app.services.index_sync.compute_enriched", lambda raw, **kwargs: raw)
    monkeypatch.setattr("app.services.index_sync._load_etf_factors", lambda repo: pl.DataFrame())

    repo = FakeRepo()
    rows = index_sync.sync_and_persist_etf_daily(
        repo,
        capset(),
        start_date=datetime(2026, 7, 1),
        end_date=datetime(2026, 7, 1),
        symbols_override=["510300.ETF"],
    )

    assert rows == 1
    assert provider.calls == [(("510300.ETF",), "etf")]
