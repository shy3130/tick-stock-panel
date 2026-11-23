"""自选按需补齐只发布请求标的，列表与详情共享结果，失败不破坏旧数据。"""
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import kline, watchlist
from app.services import kline_sync
from app.tickflow.capabilities import CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

ETF = "159558.SZ"
DAY = date(2026, 11, 4)


@pytest.fixture
def market(tmp_path, monkeypatch):
    repo = KlineRepository(DataStore(tmp_path))
    repo.save_etf_instruments(pl.DataFrame({"symbol": [ETF], "name": ["test ETF"]}))
    monkeypatch.setattr(repo, "get_enriched_latest", lambda **kw: (pl.DataFrame(), None))
    monkeypatch.setattr(repo, "get_enriched_latest_asset", lambda *a, **kw: (pl.DataFrame(), None))
    monkeypatch.setattr(watchlist.watchlist, "list_symbols", lambda: [{"symbol": ETF}])
    monkeypatch.setattr(kline, "_compress_on", lambda _: False)
    app = FastAPI()
    app.include_router(watchlist.router)
    app.include_router(kline.router)
    app.state.repo = repo
    app.state.capabilities = CapabilitySet()
    frame = pl.DataFrame({
        "symbol": [ETF], "date": [DAY], "open": [1.0], "high": [1.0], "low": [1.0],
        "close": [1.0], "volume": [100.0], "amount": [100.0],
    })
    calls = []

    def fetch(symbols, capset, **kwargs):
        calls.append((symbols, kwargs))
        yield frame

    monkeypatch.setattr(kline_sync, "iter_daily_frames", fetch)
    monkeypatch.setattr(kline_sync, "fetch_adj_factor_single", lambda *a, **kw: pl.DataFrame())
    return SimpleNamespace(repo=repo, client=TestClient(app), calls=calls, app=app)


def test_prepare_etf_is_shared_by_list_detail_and_local_restart(market):
    response = market.client.post("/api/watchlist/prepare", json={"symbols": [ETF]})
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "ready"
    assert market.calls[0][1]["asset_type"] == "etf"
    assert not list((market.repo.store.data_dir / "kline_daily").rglob("*.parquet"))
    row = market.client.get("/api/watchlist/enriched").json()["rows"][0]
    assert row["close"] == 1.0
    assert row["date"] == str(DAY)
    assert row["prepared_daily"] is True
    detail = market.client.get("/api/kline/daily", params={"symbol": ETF}).json()
    assert detail["rows"][-1]["close"] == 1.0
    assert len(market.calls) == 1
    # 新仓库仅保留磁盘文件，按需补齐应从本地恢复，不重复问上游。
    market.app.state.repo = KlineRepository(DataStore(market.repo.store.data_dir))
    assert market.client.post("/api/watchlist/prepare", json={"symbols": [ETF]}).json()["results"][0]["status"] == "ready"
    assert len(market.calls) == 1


@pytest.mark.parametrize("failure", ["empty", "invalid", "interrupted"])
def test_failed_preparation_preserves_files_and_reports_reason(market, monkeypatch, failure):
    root = market.repo.store.data_dir
    marker = root / "kline_daily" / "keep.bin"
    marker.parent.mkdir(exist_ok=True)
    marker.write_bytes(b"existing stock data")

    def fetch(*args, **kwargs):
        if failure != "empty":
            yield pl.DataFrame({"symbol": [ETF], "date": [DAY], "close": [float('nan')]})
        if failure == "interrupted":
            raise RuntimeError("interrupted")

    monkeypatch.setattr(kline_sync, "iter_daily_frames", fetch)
    result = market.client.post("/api/watchlist/prepare", json={"symbols": [ETF]}).json()["results"][0]
    assert result["status"] == ("no_data" if failure == "empty" else "failed")
    assert result["message"]
    assert not list((root / "kline_etf_daily").rglob("*.parquet"))
    assert marker.read_bytes() == b"existing stock data"


def test_prepare_rejects_symbols_outside_watchlist_and_oversized_batches(market):
    assert market.client.post("/api/watchlist/prepare", json={"symbols": ["600519.SH"]}).status_code == 400
    assert market.client.post("/api/watchlist/prepare", json={"symbols": [ETF] * 51}).status_code == 422


def test_prepared_snapshot_expires_on_daily_repair_and_does_not_pollute_market_snapshot(market):
    repo = market.repo
    result = market.client.post("/api/watchlist/prepare", json={"symbols": [ETF]})
    assert result.json()["results"][0]["status"] == "ready"
    assert repo.get_prepared_daily("etf", [ETF])["close"][0] == 1.0
    assert repo.get_enriched_latest_asset("etf")[0].is_empty()
    repo._bump_matrix_data_generation("etf")
    assert repo.get_prepared_daily("etf", [ETF]).is_empty()


def test_current_stock_partition_survives_etf_preparation(market):
    root = market.repo.store.data_dir
    stock = pl.DataFrame({
        "symbol": ["600519.SH"], "date": [DAY], "open": [10.0], "high": [10.0],
        "low": [10.0], "close": [10.0], "volume": [100.0], "amount": [1000.0],
    })
    market.repo.append_daily(stock)
    path = root / "kline_daily" / f"date={DAY}" / "part.parquet"
    original = path.read_bytes()
    assert market.client.post("/api/watchlist/prepare", json={"symbols": [ETF]}).json()["results"][0]["status"] == "ready"
    assert path.read_bytes() == original


def test_source_change_during_fetch_does_not_publish_old_source_rows(market, monkeypatch):
    stamp = [0]
    original = kline_sync.iter_daily_frames

    def changed(*args, **kwargs):
        yield from original(*args, **kwargs)
        stamp[0] += 1

    monkeypatch.setattr(kline_sync.preferences, "data_source_stamp", lambda: tuple(stamp))
    monkeypatch.setattr(kline_sync, "iter_daily_frames", changed)
    result = market.client.post("/api/watchlist/prepare", json={"symbols": [ETF]}).json()["results"][0]
    assert result["status"] == "busy"
    assert not list((market.repo.store.data_dir / "kline_etf_daily").rglob("*.parquet"))
