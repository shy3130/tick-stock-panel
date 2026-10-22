"""Index minute history, Beijing dates and provider-aware cache contracts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import indices
from app.market_time import CN_TZ
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

_fetch_minute_single = indices.kline_sync.fetch_minute_single


@pytest.fixture
def context(monkeypatch):
    indices._index_minute_cache.clear()
    monkeypatch.setattr(indices, "cn_today", lambda: date(2026, 9, 13))
    monkeypatch.setattr(indices.trading_day, "is_trading_day", lambda: False)
    monkeypatch.setattr(indices.preferences, "get_minute_data_provider", lambda: "custom_a")
    repo = MagicMock()
    repo.get_index_instruments.return_value = pl.DataFrame({
        "symbol": ["000001.SH"], "name": ["上证指数"],
    })
    repo.get_index_daily.return_value = pl.DataFrame({"date": [date(2026, 9, 11)]})
    fetch = MagicMock(return_value=pl.DataFrame({
        "datetime": [datetime(2026, 9, 11, 9, 35)], "close": [3000.0],
    }))
    monkeypatch.setattr(indices.kline_sync, "fetch_minute_single", fetch)
    state = SimpleNamespace(repo=repo, capabilities=MagicMock())
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    yield request, fetch
    indices._index_minute_cache.clear()


def test_historical_date_uses_index_minute_route(context):
    request, fetch = context
    day = date(2026, 9, 11)
    result = indices.get_index_minute(request, symbol="000001.SH", trade_date=day)
    fetch.assert_called_once_with(
        "000001.SH", day, asset_type="index", capset=request.app.state.capabilities,
    )
    assert result["date"] == "2026-09-11"
    assert result["source"] == "live"
    assert result["name"] == "上证指数"
    assert len(result["rows"]) == 1
    request.app.state.repo.get_index_daily.assert_not_called()


@pytest.mark.parametrize("today", [date(2026, 9, 13), date(2026, 10, 1)])
def test_default_on_holiday_uses_latest_index_daily(context, monkeypatch, today):
    request, fetch = context
    monkeypatch.setattr(indices, "cn_today", lambda: today)
    result = indices.get_index_minute(request, symbol="000001.SH", trade_date=None)
    assert result["date"] == "2026-09-11"
    assert fetch.call_args.args[1] == date(2026, 9, 11)
    assert request.app.state.repo.get_index_daily.call_args.kwargs == {"columns": ["date"]}


@pytest.mark.parametrize("trading", [True, None])
def test_default_trading_or_unknown_day_keeps_today(context, monkeypatch, trading):
    request, fetch = context
    monkeypatch.setattr(indices, "cn_today", lambda: date(2026, 9, 14))
    monkeypatch.setattr(indices.trading_day, "is_trading_day", lambda: trading)
    indices.get_index_minute(request, symbol="000001.SH", trade_date=None)
    assert fetch.call_args.args[1] == date(2026, 9, 14)
    request.app.state.repo.get_index_daily.assert_not_called()


def test_default_without_daily_history_keeps_today(context):
    request, fetch = context
    request.app.state.repo.get_index_daily.return_value = pl.DataFrame()
    result = indices.get_index_minute(request, symbol="000001.SH", trade_date=None)
    assert result["date"] == "2026-09-13"
    assert fetch.call_args.args[1] == date(2026, 9, 13)


def test_explicit_empty_date_is_not_replaced(context):
    request, fetch = context
    fetch.return_value = pl.DataFrame()
    result = indices.get_index_minute(request, symbol="000001.SH", trade_date=date(2026, 9, 13))
    assert result["rows"] == []
    assert result["source"] == "none"
    assert result["date"] == "2026-09-13"
    request.app.state.repo.get_index_daily.assert_not_called()


def test_future_date_does_not_fetch(context):
    request, fetch = context
    result = indices.get_index_minute(request, symbol="000001.SH", trade_date=date(2026, 9, 14))
    fetch.assert_not_called()
    assert result["rows"] == []


def test_cache_ttl_and_provider_switch(context, monkeypatch):
    request, fetch = context
    clock = [100.0]
    monkeypatch.setattr(indices.time, "monotonic", lambda: clock[0])
    day = date(2026, 9, 11)
    first = indices.get_index_minute(request, symbol="000001.SH", trade_date=day)
    assert indices.get_index_minute(request, symbol="000001.SH", trade_date=day) == first
    assert fetch.call_count == 1
    clock[0] += 11
    indices.get_index_minute(request, symbol="000001.SH", trade_date=day)
    assert fetch.call_count == 2
    monkeypatch.setattr(indices.preferences, "get_minute_data_provider", lambda: "custom_b")
    indices.get_index_minute(request, symbol="000001.SH", trade_date=day)
    assert fetch.call_count == 3


def test_unavailable_custom_source_does_not_poison_tickflow_cache(context, monkeypatch):
    request, fetch = context
    day = date(2026, 9, 11)
    fetch.side_effect = [pl.DataFrame(), pl.DataFrame({
        "datetime": [datetime(2026, 9, 11, 9, 35)], "close": [3000.0],
    })]
    monkeypatch.setattr(indices.preferences, "load", lambda: {"minute_data_provider": "missing_src"})
    monkeypatch.setattr(indices.preferences, "get_minute_data_provider", lambda: "tickflow")

    first = indices.get_index_minute(request, symbol="000001.SH", trade_date=day)
    assert first["source"] == "none"
    monkeypatch.setattr(indices.preferences, "load", lambda: {"minute_data_provider": "tickflow"})
    second = indices.get_index_minute(request, symbol="000001.SH", trade_date=day)

    assert second["source"] == "live"
    assert fetch.call_count == 2
    assert len(indices._index_minute_cache) == 2


def test_cache_separates_dates_and_symbols_and_stays_bounded(context, monkeypatch):
    request, fetch = context
    monkeypatch.setattr(indices, "_INDEX_MINUTE_CACHE_MAX", 2)
    for symbol, day in [("000001.SH", 10), ("000001.SH", 11), ("399001.SZ", 11)]:
        indices.get_index_minute(request, symbol=symbol, trade_date=date(2026, 9, day))
    assert fetch.call_count == 3
    assert len(indices._index_minute_cache) == 2


def test_failed_fetch_is_not_cached(context):
    request, fetch = context
    fetch.side_effect = [RuntimeError("upstream unavailable"), pl.DataFrame()]
    with pytest.raises(RuntimeError, match="upstream unavailable"):
        indices.get_index_minute(request, symbol="000001.SH", trade_date=date(2026, 9, 11))
    result = indices.get_index_minute(request, symbol="000001.SH", trade_date=date(2026, 9, 11))
    assert result["source"] == "none"
    assert fetch.call_count == 2


def test_concurrent_cache_eviction_keeps_each_response_isolated(context, monkeypatch):
    request, fetch = context
    monkeypatch.setattr(indices, "_INDEX_MINUTE_CACHE_MAX", 2)
    barrier = Barrier(8)

    def load(symbol, day, **kwargs):
        barrier.wait(timeout=5)
        return pl.DataFrame({"datetime": [datetime.combine(day, datetime.min.time())], "close": [day.day]})

    fetch.side_effect = load
    days = [date(2026, 9, day) for day in range(1, 9)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda day: indices.get_index_minute(request, symbol="000001.SH", trade_date=day), days,
        ))
    assert [result["date"] for result in results] == [day.isoformat() for day in days]
    assert [result["rows"][0]["close"] for result in results] == list(range(1, 9))
    assert len(indices._index_minute_cache) == 2


def test_http_date_alias_and_default(context):
    request, fetch = context
    app = FastAPI()
    app.state.repo = request.app.state.repo
    app.state.capabilities = request.app.state.capabilities
    app.include_router(indices.router)
    with TestClient(app) as client:
        result = client.get("/api/index/minute", params={"symbol": "000001.SH", "date": "2026-09-11"})
        assert result.status_code == 200
        assert result.json()["rows"][0]["datetime"] == "2026-09-11T09:35:00"
        assert client.get("/api/index/minute?symbol=000001.SH").json()["date"] == "2026-09-11"
        assert client.get("/api/index/minute?symbol=000001.SH&date=invalid").status_code == 422
    fetch.assert_called_once()


@pytest.mark.parametrize("native_allowed", [False, True])
def test_history_preserves_native_permissions_and_requested_window(context, monkeypatch, native_allowed):
    request, _ = context
    request.app.state.capabilities = CapabilitySet(
        {Cap.KLINE_MINUTE_BY_SYMBOL: CapabilityLimits()} if native_allowed else {},
    )
    monkeypatch.setattr(indices.kline_sync, "fetch_minute_single", _fetch_minute_single)
    custom = MagicMock(return_value=(None, True))
    monkeypatch.setattr(indices.kline_sync, "_try_custom_minute", custom)
    client = MagicMock()
    client.klines.batch.return_value = []
    get_client = MagicMock(return_value=client)
    monkeypatch.setattr(indices.kline_sync, "get_client", get_client)
    day = date(2026, 9, 11)
    first = indices.get_index_minute(request, symbol="000001.SH", trade_date=day)
    assert first["rows"] == []
    assert indices.get_index_minute(request, symbol="000001.SH", trade_date=day) == first
    custom.assert_called_once()
    assert custom.call_args.kwargs["asset_type"] == "index"
    if not native_allowed:
        get_client.assert_not_called()
        return
    client.klines.batch.assert_called_once()
    args, kwargs = client.klines.batch.call_args
    assert args == (["000001.SH"],)
    assert kwargs["period"] == "1m"
    assert kwargs["start_time"] == int(datetime(2026, 9, 11, 9, 25, tzinfo=CN_TZ).timestamp() * 1000)
    assert kwargs["end_time"] == int(datetime(2026, 9, 11, 15, 5, tzinfo=CN_TZ).timestamp() * 1000)
