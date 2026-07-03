from datetime import datetime
from types import SimpleNamespace

import polars as pl

from app.api import kline


class FakeRepo:
    def __init__(self):
        self.daily_calls = 0
        self.batch_calls = 0

    def get_daily(self, symbol, start, end):
        self.daily_calls += 1
        return pl.DataFrame()

    def get_daily_batch(self, symbols, start, end, columns=None):
        self.batch_calls += 1
        return pl.DataFrame()

    def execute_one(self, sql, params=None):
        return None


class FakeProvider:
    def __init__(self):
        self.daily_args = None
        self.daily_calls = []
        self.adj_args = None

    def get_daily(self, symbols, start_time, end_time, asset_type):
        self.daily_args = (symbols, start_time, end_time, asset_type)
        self.daily_calls.append((symbols, start_time, end_time, asset_type))
        return pl.DataFrame({
            "symbol": [symbols[0]],
            "date": [start_time.date()],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [100.0],
            "amount": [100.0],
        })

    def get_adj_factors(self, symbols, start_time, end_time, asset_type):
        self.adj_args = (symbols, start_time, end_time, asset_type)
        return pl.DataFrame()


class CachedRepo(FakeRepo):
    def get_daily(self, symbol, start, end):
        self.daily_calls += 1
        return pl.DataFrame({
            "symbol": [symbol],
            "date": [start],
            "open": [9.0],
            "high": [9.0],
            "low": [9.0],
            "close": [9.0],
            "volume": [9.0],
        })

    def get_daily_batch(self, symbols, start, end, columns=None):
        self.batch_calls += 1
        return pl.DataFrame({
            "symbol": [symbols[0]],
            "date": [start],
            "open": [9.0],
            "high": [9.0],
            "low": [9.0],
            "close": [9.0],
            "volume": [9.0],
        })


def request(repo=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo or FakeRepo(), quote_service=None)))


def test_daily_local_fallback_passes_datetime_to_provider(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-02",
        ext_columns=None,
    )

    assert resp["source"] == "local_disk"
    assert isinstance(provider.daily_args[1], datetime)
    assert isinstance(provider.daily_args[2], datetime)
    assert provider.daily_args[3] == "stock"
    assert isinstance(provider.adj_args[1], datetime)
    assert isinstance(provider.adj_args[2], datetime)
    assert provider.adj_args[3] == "stock"


def test_daily_local_fallback_passes_hk_asset_type_and_skips_adj(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(),
        "02577.HK",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-02",
        ext_columns=None,
    )

    assert resp["source"] == "local_disk"
    assert provider.daily_args[0] == ["02577.HK"]
    assert provider.daily_args[3] == "hk"
    assert provider.adj_args is None


def test_daily_batch_local_fallback_passes_datetime_to_provider(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily_batch(request(), {"symbols": ["600519.SH"], "days": 5})

    assert "600519.SH" in resp["data"]
    assert isinstance(provider.daily_args[1], datetime)
    assert isinstance(provider.daily_args[2], datetime)
    assert provider.daily_args[3] == "stock"


def test_daily_batch_local_fallback_splits_asset_types(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily_batch(request(), {"symbols": ["600519.SH", "513050.SH", "02577.HK"], "days": 5})

    assert set(resp["data"]) == {"600519.SH", "513050.SH", "02577.HK"}
    assert [(args[0], args[3]) for args in provider.daily_calls] == [
        (["600519.SH"], "stock"),
        (["513050.SH"], "etf"),
        (["02577.HK"], "hk"),
    ]


def test_daily_local_mode_ignores_cached_raw(monkeypatch):
    provider = FakeProvider()
    repo = CachedRepo()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily(
        request(repo),
        "600519.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-02",
        ext_columns=None,
    )

    assert repo.daily_calls == 0
    assert resp["source"] == "local_disk"
    assert resp["rows"][0]["close"] == 1.0


def test_daily_batch_local_mode_ignores_cached_raw(monkeypatch):
    provider = FakeProvider()
    repo = CachedRepo()
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    resp = kline.get_daily_batch(request(repo), {"symbols": ["600519.SH"], "days": 5})

    assert repo.batch_calls == 0
    assert resp["data"]["600519.SH"][0]["close"] == 1.0
