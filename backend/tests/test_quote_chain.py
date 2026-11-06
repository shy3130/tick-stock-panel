"""Provider -> per-asset merge -> cache -> list/detail/latest, with no reader fetches."""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import intraday, kline
from app.api import watchlist as watchlist_api
from app.market_time import CN_TZ
from app.services import preferences, quote_service, watchlist
from app.tickflow.capabilities import CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository


def test_three_assets_share_publication_and_readers_preserve_other_symbols(tmp_path, monkeypatch):
    today, yesterday = date(2026, 11, 6), date(2026, 11, 5)
    now = datetime(2026, 11, 6, 10, 0, 10, tzinfo=CN_TZ)
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    monkeypatch.setattr(quote_service, "cn_today", lambda: today)
    monkeypatch.setattr(kline, "cn_today", lambda: today)
    monkeypatch.setattr("app.services.quote_freshness.cn_now", lambda: now)
    monkeypatch.setattr(preferences, "load", lambda: {"realtime_data_provider": "tickflow"})
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tickflow")
    monkeypatch.setattr(preferences, "get_realtime_pull_stock", lambda: False)
    monkeypatch.setattr(preferences, "get_realtime_pull_etf", lambda: False)
    monkeypatch.setattr(quote_service, "_persist_last_fetch", lambda *a: None)
    monkeypatch.setattr("app.services.trading_day.cached_verdict", lambda *a: True)
    targets = {"stock": "600519.SH", "etf": "159558.SZ", "index": "000985.SH"}
    others = {"stock": "600000.SH", "etf": "510300.SH", "index": "000001.SH"}
    monkeypatch.setattr(watchlist, "list_symbols", lambda: [{"symbol": s} for s in targets.values()])
    repo = KlineRepository(DataStore(tmp_path))
    repo.save_etf_instruments(pl.DataFrame({"symbol": [targets['etf'], others['etf']], "name": ["ETF", "other ETF"]}))
    repo.save_index_instruments(pl.DataFrame({"symbol": [targets['index'], others['index']], "name": ["index", "other index"], "asset_type": ["index", "index"]}))
    for asset, symbol in targets.items():
        rows = pl.DataFrame([
            {"symbol": symbol, "date": yesterday, "open": 9., "high": 9., "low": 9., "close": 9., "volume": 100., "amount": 900.},
            {"symbol": others[asset], "date": today, "open": 8., "high": 8., "low": 8., "close": 8., "volume": 100., "amount": 800.},
        ])
        repo.append_daily_asset(asset, rows)
        repo.append_enriched_asset(asset, rows)
    repo.refresh_index_views()
    old_partitions = {p: p.read_bytes() for p in tmp_path.glob("kline*/date=2026-11-05/*.parquet")}
    stamp = int(now.timestamp() * 1000) - 10_000
    records = [{"symbol": s, "last_price": 10., "prev_close": 9., "open": 9., "high": 10., "low": 9.,
                "volume": 200., "amount": 2000., "timestamp": stamp} for s in targets.values()]
    quotes = SimpleNamespace(get_by_universes=Mock(), get=Mock(return_value=records))
    monkeypatch.setattr("app.tickflow.client.get_paid_realtime_client", lambda: SimpleNamespace(quotes=quotes))
    service = quote_service.QuoteService()
    service.set_repo(repo)
    service._enabled = True
    assert service._fetch_quotes()
    app = FastAPI()
    app.include_router(intraday.router)
    app.include_router(kline.router)
    app.include_router(watchlist_api.router)
    app.state.repo, app.state.quote_service, app.state.capabilities = repo, service, CapabilitySet()
    client = TestClient(app)
    listed = client.get("/api/watchlist/enriched").json()["rows"]
    assert len(listed) == 3
    for asset, symbol in targets.items():
        row = next(r for r in listed if r['symbol'] == symbol)
        assert row['close'] == 10.
        assert row['asset_type'] == asset
        assert row['quote_freshness']['state'] == 'live'
        detail = client.get("/api/kline/daily", params={"symbol": symbol, "days": 10}).json()
        assert detail['rows'][-1]['date'] == str(today)
        assert detail['rows'][-1]['close'] == 10.
        assert detail['quote_freshness']['state'] == 'live'
        latest = client.get("/api/kline/daily/latest", params={"symbol": symbol}).json()
        assert latest['row']['quote_ts'] == stamp
        folder = {'stock': 'kline_daily_enriched', 'etf': 'kline_etf_enriched', 'index': 'kline_index_enriched'}[asset]
        current = pl.read_parquet(tmp_path / folder / f'date={today}' / 'part.parquet')
        assert current.filter(pl.col('symbol') == others[asset])['close'][0] == 8.
    quotes.get.assert_called_once()
    quotes.get_by_universes.assert_not_called()
    assert old_partitions and all(p.read_bytes() == content for p, content in old_partitions.items())
