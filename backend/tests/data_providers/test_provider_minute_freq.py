from datetime import datetime

import pytest

from app.data_providers.fquant.catalog_resolver import RouteNotFoundError, StaleCatalogError
from app.data_providers.fquant_provider import FQuantProvider


class FakeMinuteEngine:
    def __init__(self):
        self.calls = []
        self.throw_on = None
        self.throw_exc = RouteNotFoundError

    def get_minutes(self, code, date_yyyymmdd, limit=5000, asset_type=None):
        self.calls.append((code, date_yyyymmdd, asset_type))
        if self.throw_on == date_yyyymmdd:
            raise self.throw_exc(f"catalog failure for {date_yyyymmdd}")
        return [
            {"price": 10.0, "volume": 1},
            {"price": 12.0, "volume": 2},
            {"price": 9.0, "volume": 3},
            {"price": 11.0, "volume": 4},
            {"price": 13.0, "volume": 5},
            {"price": 14.0, "volume": 6},
        ]

def test_get_minute_aggregates_requested_freq():
    engine = FakeMinuteEngine()
    provider = object.__new__(FQuantProvider)
    provider._engine = engine
    provider.name = "fquant"

    df = provider.get_minute(
        ["600519.SH"],
        datetime(2026, 7, 1),
        datetime(2026, 7, 1),
        "stock",
        freq="5m",
    )

    rows = df.to_dicts()
    assert len(engine.calls) == 1
    c = engine.calls[0]
    assert c[0] == "600519" and c[1] == "20260701" and c[2] == "stock"
    assert len(rows) == 2
    assert rows[0]["datetime"] == "2026-07-01 09:35:00"
    assert rows[0]["freq"] == "5m"

def test_get_minute_multi_day_merges_different_catalog_routes():
    """跨两日不同 route/date + 显式 asset_type + 合并 (provider 逐日 catalog)。"""
    engine = FakeMinuteEngine()
    provider = object.__new__(FQuantProvider)
    provider._engine = engine
    provider.name = "fquant"
    df = provider.get_minute(
        ["600519.SH"],
        datetime(2026, 7, 1, 9, 25),
        datetime(2026, 7, 2, 15, 5),
        "stock",
        freq="1m",
    )
    assert len(engine.calls) == 2
    assert engine.calls[0] == ("600519", "20260701", "stock")
    assert engine.calls[1] == ("600519", "20260702", "stock")
    assert not df.is_empty()
    dates = sorted(df["datetime"].str.slice(0, 10).unique().to_list())
    assert dates == ["2026-07-01", "2026-07-02"]

def test_get_minute_propagates_second_day_route_not_found():
    """第二日 RouteNotFound 原样传播 (缺中间日契约)。"""
    engine = FakeMinuteEngine()
    engine.throw_on = "20260702"
    provider = object.__new__(FQuantProvider)
    provider._engine = engine
    provider.name = "fquant"
    with pytest.raises(RouteNotFoundError):
        provider.get_minute(
            ["600519.SH"],
            datetime(2026, 7, 1),
            datetime(2026, 7, 2),
            "stock",
            freq="1m",
        )

def test_get_minute_propagates_second_day_stale_catalog():
    """第二日 StaleCatalogError 原样传播 (stale 契约)。"""
    engine = FakeMinuteEngine()
    engine.throw_on = "20260702"
    engine.throw_exc = StaleCatalogError
    provider = object.__new__(FQuantProvider)
    provider._engine = engine
    provider.name = "fquant"
    with pytest.raises(StaleCatalogError):
        provider.get_minute(
            ["600519.SH"],
            datetime(2026, 7, 1),
            datetime(2026, 7, 2),
            "stock",
            freq="1m",
        )
