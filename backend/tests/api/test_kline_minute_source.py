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

from app.services.external_fallback.adapter import ChartFallbackResult


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


def test_minute_local_target_day_empty_uses_chart_live_display_fallback(monkeypatch):
    class EmptyProvider:
        def get_minute(self, *args, **kwargs):
            return pl.DataFrame()

    class ChartAdapter:
        def __init__(self):
            self.calls = []

        def resolve_chart_live(self, symbol, trade_date, *, local_rows_empty):
            self.calls.append((symbol, trade_date, local_rows_empty))
            return ChartFallbackResult(
                minutes=[{
                    "datetime": "2026-08-24 09:30:00",
                    "time": "09:30",
                    "open": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "close": 10.0,
                    "volume": 100.0,
                    "amount": 1000.0,
                    "source": "tencent_chart",
                    "provisional": True,
                }],
                daily={
                    "date": "2026-08-24",
                    "open": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "close": 10.0,
                    "volume": 100.0,
                    "amount": 1000.0,
                    "source": "tencent_chart",
                    "provisional": True,
                    "is_live": True,
                },
                used_fallback=True,
                source="tencent_chart",
            )

    adapter = ChartAdapter()
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda scope: "fquant_local",
    )
    monkeypatch.setattr(
        "app.data_providers.registry.get_provider", lambda name: EmptyProvider()
    )
    monkeypatch.setattr(
        "app.services.external_fallback.get_adapter", lambda: adapter
    )

    resp = kline.get_minute(request(), "600519.SH", date(2026, 8, 24))

    assert resp["source"] == "tencent_chart"
    assert resp["degraded"] is True
    assert resp["sources"] == {"chart_live": "tencent_chart"}
    assert resp["rows"][0]["provisional"] is True
    assert adapter.calls == [("600519.SH", date(2026, 8, 24), True)]


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


def test_minute_catalog_errors_never_invoke_chart_live_fallback(monkeypatch):
    class FallbackMustNotRun:
        def resolve_chart_live(self, *args, **kwargs):
            raise AssertionError("catalog failures must remain fail-closed")

    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda scope: "fquant_local",
    )
    monkeypatch.setattr(
        "app.services.external_fallback.get_adapter", lambda: FallbackMustNotRun()
    )

    class BadProvider:
        def get_minute(self, *args, **kwargs):
            raise StaleCatalogError("test catalog error")

    monkeypatch.setattr(
        "app.data_providers.registry.get_provider", lambda name: BadProvider()
    )
    with pytest.raises(HTTPException) as exc:
        kline.get_minute(request(), "600519.SH", date(2026, 7, 2))
    assert exc.value.status_code == 503
