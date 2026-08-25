from datetime import date
from types import SimpleNamespace

import pytest
import polars as pl
from fastapi import HTTPException

from app.api import kline
from app.data_providers.fquant.catalog_resolver import (
    RouteNotFoundError,
    StaleCatalogError,
)


class FakeRepo:
    def execute_one(self, sql, params=None):
        return None

    def latest_minute_date(self, symbol):
        return None

    def latest_daily_date(self):
        return date(2026, 7, 3)


def request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=FakeRepo())))


def test_minute_historical_uses_registry_provider(monkeypatch):
    calls = {"provider": 0}

    class Provider:
        def get_minute(self, symbols, start_time, end_time, asset_type, freq="1m"):
            calls["provider"] += 1
            return pl.DataFrame({
                "symbol": symbols,
                "datetime": ["2026-07-02 09:31:00"],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [100.0],
                "amount": [100.0],
            })

    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda scope: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: Provider())

    resp = kline.get_minute(request(), "600519.SH", date(2026, 7, 2))

    assert resp["source"] == "fquant_local"
    assert len(resp["rows"]) == 1
    assert calls == {"provider": 1}


def test_minute_catalog_errors_map_to_503(monkeypatch):
    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda scope: "fquant_local")

    for Error in (RouteNotFoundError, StaleCatalogError):
        class BadProvider:
            def get_minute(self, *a, **k):
                raise Error("test catalog error")

        monkeypatch.setattr("app.data_providers.registry.get_provider", lambda n: BadProvider())

        with pytest.raises(HTTPException) as exc:
            kline.get_minute(request(), "600519.SH", date(2026, 7, 2))
        assert exc.value.status_code == 503
        assert "Retry-After" in exc.value.headers
