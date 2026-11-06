from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest

from app.services import preferences, quote_service, watchlist
from app.services.index_const import CORE_INDEX_SYMBOLS


@pytest.fixture
def market(monkeypatch, tmp_path):
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [
        {"symbol": "000985.SH"}, {"symbol": "159558.SZ"}, {"symbol": "600519.SH"},
    ])
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tickflow")
    monkeypatch.setattr(preferences, "get_realtime_pull_stock", lambda: True)
    monkeypatch.setattr(preferences, "get_realtime_pull_etf", lambda: False)
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_index_symbol_set=lambda: set(CORE_INDEX_SYMBOLS) | {"000985.SH", "000300.SH"},
        get_etf_symbol_set=lambda: {"159558.SZ", "510300.SH"},
        get_etf_instruments=lambda: pl.DataFrame({"symbol": ["159558.SZ", "510300.SH"]}),
    )
    service = quote_service.QuoteService()
    service.set_repo(repo)
    captured = []
    monkeypatch.setattr(service, "_process_full_market_records", lambda records, **kw: captured.append((records, kw)))
    quotes = SimpleNamespace(
        get_by_universes=Mock(return_value=[{"symbol": "600519.SH", "last_price": 10}]),
        get=Mock(side_effect=lambda *, symbols: [{"symbol": s, "last_price": 20} for s in symbols]),
    )
    monkeypatch.setattr("app.tickflow.client.get_paid_realtime_client", lambda: SimpleNamespace(quotes=quotes))
    return service, quotes, captured


def test_watchlist_indices_and_etfs_join_shared_poll_without_repulling_stock(market):
    service, quotes, captured = market
    service._fetch_full_market_quotes()
    explicit = set(quotes.get.call_args.kwargs["symbols"])
    assert {"000985.SH", "159558.SZ"} <= explicit
    assert "600519.SH" not in explicit
    assert "510300.SH" not in explicit
    assert captured[0][1]["partial_assets"] == {"etf"}


def test_windows_share_union_and_closing_one_does_not_release_another(market):
    service, quotes, _ = market
    service.subscribe_quotes("window-a", ["000300.SH", "510300.SH"])
    service.subscribe_quotes("window-b", ["000300.SH"])
    service.unsubscribe_quotes("window-a")
    service._fetch_full_market_quotes()
    symbols = quotes.get.call_args.kwargs["symbols"]
    assert symbols.count("000300.SH") == 1
    assert "510300.SH" not in symbols
    service.unsubscribe_quotes("window-b")
    service._fetch_full_market_quotes()
    assert "000300.SH" not in quotes.get.call_args.kwargs["symbols"]


def test_detail_demand_can_change_asset_and_expires(market, monkeypatch):
    from app.services import quote_demand

    tick = [100.0]
    monkeypatch.setattr(quote_demand.time, "monotonic", lambda: tick[0])
    service, quotes, _ = market
    service.subscribe_quotes("detail", ["000300.SH"])
    service.subscribe_quotes("detail", ["510300.SH"])
    service._fetch_full_market_quotes()
    assert "510300.SH" in quotes.get.call_args.kwargs["symbols"]
    assert "000300.SH" not in quotes.get.call_args.kwargs["symbols"]
    tick[0] += 91
    service._fetch_full_market_quotes()
    assert "510300.SH" not in quotes.get.call_args.kwargs["symbols"]


def test_stock_demand_works_without_bulk_universe(market, monkeypatch):
    monkeypatch.setattr(preferences, "get_realtime_pull_stock", lambda: False)
    service, quotes, captured = market
    service._fetch_full_market_quotes()
    quotes.get_by_universes.assert_not_called()
    assert "600519.SH" in quotes.get.call_args.kwargs["symbols"]
    assert captured[0][1]["partial_assets"] == {"stock", "etf"}


def test_custom_index_supplement_includes_watchlist_and_detail(market, monkeypatch):
    service, _, captured = market
    provider = SimpleNamespace(get_realtime=lambda: [{"symbol": "600519.SH", "last_price": 10}],
                               get_realtime_indices=Mock(return_value=[]))
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "custom")
    monkeypatch.setattr("app.data_providers.custom.provider_has_dataset", lambda *a: True)
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda *a: provider)
    service.subscribe_quotes("detail", ["000300.SH"])
    service._fetch_full_market_quotes()
    assert {"000985.SH", "000300.SH"} <= set(provider.get_realtime_indices.call_args.args[0])
    assert captured


def test_custom_targeted_quotes_follow_asset_route_without_duplicate_stock_fetch(market, monkeypatch):
    service, quotes, captured = market
    provider = SimpleNamespace(
        get_realtime=lambda: [{"symbol": "600519.SH", "last_price": 10}],
        get_realtime_symbols=Mock(return_value=[{"symbol": "159558.SZ", "last_price": .827}]),
    )
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "custom")
    monkeypatch.setattr("app.data_providers.custom.provider_has_dataset", lambda *a: True)
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda *a: provider)
    service._fetch_full_market_quotes()
    provider.get_realtime_symbols.assert_called_once_with(["159558.SZ"], asset_type="etf")
    assert captured[0][1]["partial_assets"] == {"etf"}
    quotes.get.assert_not_called()


def test_partial_quotes_merge_both_raw_and_enriched_instead_of_replacing(market, monkeypatch):
    service, _, _ = market
    # Exercise the real publication path, isolated from the market worker.
    del service._process_full_market_records
    repo = service._repo
    writes = []
    repo.merge_live_daily_asset = lambda asset, frame: writes.append(("raw", asset))
    repo.flush_live_daily = Mock(side_effect=AssertionError("partial stock must merge"))
    repo.flush_live_daily_asset = Mock(side_effect=AssertionError("partial ETF must merge"))
    monkeypatch.setattr(service, "_flush_live_enriched", lambda frame, extra, *, asset_type, merge=False: writes.append(("enriched", asset_type, merge)))
    monkeypatch.setattr(service, "_broadcast_quote_updated", lambda: None)
    monkeypatch.setattr(service, "_update_volume_delta", lambda *a: None)
    monkeypatch.setattr(quote_service, "_persist_last_fetch", lambda *a: None)
    rows = [{"symbol": s, "last_price": 10., "open": 10., "high": 11., "low": 9., "volume": 100.}
            for s in ["600519.SH", "159558.SZ"]]
    service._process_full_market_records(rows, t0=0., now_ts=0., partial_assets={"stock", "etf"})
    assert writes == [("raw", "stock"), ("raw", "etf"), ("enriched", "stock", True), ("enriched", "etf", True)]


def test_subscription_api_only_registers_and_validates_no_inline_fetch(market):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.intraday import router

    service, quotes, _ = market
    app = FastAPI()
    app.include_router(router)
    app.state.quote_service = service
    client = TestClient(app)
    assert client.put("/api/intraday/subscriptions/detail", json={"symbols": ["000300.sh"]}).json()["lease_seconds"] == 90
    quotes.get.assert_not_called()
    service._fetch_full_market_quotes()
    assert "000300.SH" in quotes.get.call_args.kwargs["symbols"]
    assert client.put("/api/intraday/subscriptions/detail", json={"symbols": ["invalid"]}).status_code == 400
    assert client.put("/api/intraday/subscriptions/detail", json={"symbols": ["000300.SH"] * 65}).status_code == 422
    assert client.delete("/api/intraday/subscriptions/detail").status_code == 200
    assert client.delete("/api/intraday/subscriptions/detail").status_code == 200
    service._fetch_full_market_quotes()
    assert "000300.SH" not in quotes.get.call_args.kwargs["symbols"]


@pytest.mark.parametrize("failed", [False, True])
def test_empty_or_failed_supplement_keeps_market_snapshot_working(market, failed):
    service, quotes, captured = market
    quotes.get.return_value = []
    quotes.get.side_effect = RuntimeError("supplement unavailable") if failed else None
    service._fetch_full_market_quotes()
    assert captured[0][0][0]["symbol"] == "600519.SH"


def test_bad_extra_symbol_does_not_block_core_index_refresh(market):
    service, quotes, captured = market

    def fetch(*, symbols):
        if "159558.SZ" in symbols:
            raise ValueError("unsupported extra")
        return [{"symbol": s, "last_price": 20} for s in symbols]

    quotes.get.side_effect = fetch
    service._fetch_full_market_quotes()
    assert set(CORE_INDEX_SYMBOLS) <= {r["symbol"] for r in captured[0][0]}


def test_unavailable_custom_realtime_source_never_falls_back(market, monkeypatch):
    service, quotes, captured = market
    monkeypatch.setattr(preferences, "load", lambda: {"realtime_data_provider": "removed-source"})
    monkeypatch.setattr("app.data_providers.custom.provider_has_dataset", lambda *a: False)
    service._fetch_full_market_quotes()
    quotes.get.assert_not_called()
    quotes.get_by_universes.assert_not_called()
    assert not captured


def test_simultaneous_manual_and_worker_refresh_share_one_upstream_round(market, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    service, _, _ = market
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fetch(**kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        service._fetched_at += 1

    monkeypatch.setattr(service, "_fetch_full_market_quotes", fetch)
    with ThreadPoolExecutor(4) as pool:
        first = pool.submit(service._fetch_quotes)
        assert entered.wait(3)
        others = [pool.submit(service._fetch_quotes) for _ in range(3)]
        release.set()
        assert first.result(3)
        assert all(future.result(3) for future in others)
    assert calls == [1]
    service.subscribe_quotes("new-detail", ["000300.SH"])
    assert service._fetch_quotes()
    assert calls == [1, 1]


def test_paused_worker_cannot_be_forced_to_write_by_refresh(market, monkeypatch):
    service, _, _ = market
    fetch = Mock()
    monkeypatch.setattr(service, "_fetch_full_market_quotes", fetch)
    service.pause()
    assert service._fetch_quotes() is False
    fetch.assert_not_called()


def test_last_fetch_persistence_does_not_change_source_version(monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    monkeypatch.setattr(preferences, "load", lambda: {"daily_data_provider": "tickflow", "last_fetch_ms": 100})
    first = preferences.data_source_stamp()
    monkeypatch.setattr(preferences, "load", lambda: {"daily_data_provider": "tickflow", "last_fetch_ms": 200})
    assert preferences.data_source_stamp() == first
    monkeypatch.setattr(preferences, "load", lambda: {"daily_data_provider": "fuyao", "last_fetch_ms": 200})
    assert preferences.data_source_stamp() != first


def test_source_change_while_fetching_does_not_publish_old_quotes(market, monkeypatch):
    service, quotes, captured = market
    version = [0]
    monkeypatch.setattr(preferences, "data_source_stamp", lambda: (version[0],))
    original = quotes.get.side_effect

    def changed(**kwargs):
        version[0] += 1
        return original(**kwargs)

    quotes.get.side_effect = changed
    service._fetch_full_market_quotes()
    assert not captured


def test_many_visible_symbols_respect_capability_batch_limit(market, monkeypatch):
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

    service, quotes, _ = market
    service.set_app_state(SimpleNamespace(capabilities=CapabilitySet({Cap.QUOTE_BATCH: CapabilityLimits(batch=2)})))
    service._fetch_full_market_quotes()
    batches = [call.kwargs['symbols'] for call in quotes.get.call_args_list]
    assert len(batches) > 1
    assert all(len(batch) <= 2 for batch in batches)
    flat = [s for batch in batches for s in batch]
    assert len(flat) == len(set(flat))
    assert {'000985.SH', '159558.SZ'} <= set(flat)


def test_temporarily_unreadable_watchlist_keeps_market_and_detail_demand(market, monkeypatch):
    service, quotes, _ = market
    service._fetch_full_market_quotes()
    path = service._repo.store.data_dir / 'user_data' / 'watchlist.parquet'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'unreadable concurrent replacement')
    monkeypatch.setattr(watchlist, 'list_symbols', Mock(side_effect=OSError('temporarily unavailable')))
    service.subscribe_quotes('detail', ['000300.SH'])
    service._fetch_full_market_quotes()
    assert {'000985.SH', '000300.SH', '159558.SZ'} <= set(quotes.get.call_args.kwargs['symbols'])
