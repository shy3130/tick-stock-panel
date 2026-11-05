"""详情缺数补拉必须遵循日K/复权数据源及查询窗口。"""
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest

from app.api import kline as api
from app.data_providers import custom
from app.services import kline_sync
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

ETF = "159558.SZ"
DAY = date(2026, 11, 4)


@pytest.mark.parametrize("asset_type", ["stock", "etf", "index"])
@pytest.mark.parametrize("has_rows", [False, True])
def test_daily_metadata_carries_asset_type_without_rounding_prices(monkeypatch, asset_type, has_rows):
    from app.services import daily_bootstrap

    frame = pl.DataFrame({"date": [DAY], "open": [0.751], "high": [0.828], "low": [0.747], "close": [0.827]})
    repo = SimpleNamespace(resolve_asset_type=lambda symbol: asset_type, get_daily_asset=lambda *args: frame if has_rows else pl.DataFrame())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)), headers={})
    monkeypatch.setattr(api, "_get_stock_info", lambda *args: {})
    monkeypatch.setattr(api, "_get_asset_info", lambda *args: {})
    monkeypatch.setattr(api, "_compress_on", lambda *args: False)
    monkeypatch.setattr(daily_bootstrap, "ensure_history", lambda *args, **kwargs: pl.DataFrame())
    result = api.get_daily(request, ETF, 120, str(DAY), str(DAY), None)
    assert result["asset_type"] == asset_type
    if has_rows:
        assert result["rows"][0]["close"] == 0.827
    else:
        assert result["rows"] == []


@pytest.fixture
def route(monkeypatch, tmp_path):
    frame = pl.DataFrame({
        "symbol": [ETF], "date": [DAY], "open": [1.0], "high": [1.0], "low": [1.0],
        "close": [1.0], "volume": [100.0], "amount": [100.0],
    })
    repo = KlineRepository(DataStore(tmp_path))
    repo.save_etf_instruments(pl.DataFrame({"symbol": [ETF], "name": ["test"]}))
    monkeypatch.setattr(repo, "get_daily_asset", lambda *a: pl.DataFrame())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, capabilities=CapabilitySet())), headers={})
    calls = []

    class Provider:
        def get_daily(self, symbols, **kwargs):
            calls.append((symbols, kwargs))
            return frame

    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "test")
    monkeypatch.setattr(custom, "provider_has_dataset", lambda name, dataset: dataset == "daily")
    monkeypatch.setattr(custom, "get_provider", lambda _: Provider())
    tickflow = Mock(side_effect=AssertionError("must not bypass the selected provider"))
    monkeypatch.setattr(kline_sync, "get_client", tickflow)
    monkeypatch.setattr(api, "_get_asset_info", lambda *a: {"name": "test ETF"})
    monkeypatch.setattr(api, "_maybe_inject_live_candle", lambda request, symbol, rows, asset: rows)
    monkeypatch.setattr(api, "_compress_on", lambda _: False)
    return request, calls, tickflow


def test_detail_daily_uses_selected_provider_and_asset(route):
    request, calls, tickflow = route
    result = api.get_daily(request, ETF, 120, str(DAY), str(DAY), None)
    assert result["asset_type"] == "etf"
    assert result["rows"][0]["date"] == DAY
    assert calls[0][0] == [ETF]
    assert calls[0][1]["asset_type"] == "etf"
    assert calls[0][1]["start_time"].date() <= DAY
    assert calls[0][1]["end_time"].date() == DAY
    tickflow.assert_not_called()


def test_detail_does_not_silently_switch_when_selected_provider_fails(route, monkeypatch):
    request, _, tickflow = route

    def broken(*args, **kwargs):
        raise RuntimeError("test source failed")

    monkeypatch.setattr(custom, "get_provider", broken)
    with pytest.raises(api.HTTPException) as error:
        api.get_daily(request, ETF, 120, str(DAY), str(DAY), None)
    assert error.value.status_code == 502
    tickflow.assert_not_called()


def test_detail_adjustment_uses_selected_source_without_tickflow_permission(monkeypatch):
    factors = pl.DataFrame({"symbol": [ETF], "trade_date": [DAY], "ex_factor": [1.25]})
    get_factors = Mock(return_value=factors)
    monkeypatch.setattr(kline_sync.preferences, "get_adj_factor_provider", lambda: "test")
    monkeypatch.setattr(custom, "provider_has_dataset", lambda *a: True)
    monkeypatch.setattr(custom, "get_provider", lambda _: SimpleNamespace(get_adj_factors=get_factors))
    monkeypatch.setattr(kline_sync, "get_client", Mock(side_effect=AssertionError("wrong source")))
    assert kline_sync.fetch_adj_factor_single(ETF, asset_type="etf", capset=CapabilitySet()).equals(factors)
    assert get_factors.call_args.kwargs["asset_type"] == "etf"


def test_adjustment_failure_cannot_be_published_as_unadjusted_history(monkeypatch):
    capset = CapabilitySet()
    capset.grant(Cap.ADJ_FACTOR)
    monkeypatch.setattr(kline_sync.preferences, "get_adj_factor_provider", lambda: "tickflow")
    monkeypatch.setattr(kline_sync, "get_client", lambda: SimpleNamespace(
        klines=SimpleNamespace(ex_factors=Mock(side_effect=RuntimeError("source failed"))),
    ))
    with pytest.raises(kline_sync.ProviderRequestError, match="复权因子"):
        kline_sync.fetch_adj_factor_single(ETF, capset=capset, strict=True)
