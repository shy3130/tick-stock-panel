"""个股详情日 K 最新行接口测试。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.kline import router


class _FakeQuoteService:
    def __init__(self, frame: pl.DataFrame, trade_date: date | None) -> None:
        self._frame = frame
        self._trade_date = trade_date

    def get_enriched_today(self):
        return self._frame, self._trade_date


class _FakeRepo:
    def __init__(
        self,
        asset_type: str = "stock",
        latest_asset: tuple[pl.DataFrame, date | None] | None = None,
    ) -> None:
        self.asset_type = asset_type
        self.latest_asset = latest_asset
        self.latest_asset_calls = 0
        self.latest_asset_refresh: bool | None = None

    def resolve_asset_type(self, symbol: str) -> str:
        return self.asset_type

    def get_enriched_latest_asset(self, asset_type: str, refresh: bool = True):
        self.latest_asset_calls += 1
        self.latest_asset_refresh = refresh
        if self.latest_asset is not None:
            return self.latest_asset
        raise AssertionError("stock latest row must use QuoteService's memory cache")


def _client(
    frame: pl.DataFrame,
    trade_date: date | None,
    *,
    asset_type: str = "stock",
    latest_asset: tuple[pl.DataFrame, date | None] | None = None,
) -> tuple[TestClient, _FakeRepo]:
    repo = _FakeRepo(asset_type, latest_asset)
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    app.state.quote_service = _FakeQuoteService(frame, trade_date)
    return TestClient(app), repo


def _live_frame() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [date.today()],
        "open": [10.0],
        "high": [10.8],
        "low": [9.9],
        "close": [10.6],
        "volume": [123_456.0],
        "amount": [1_234_560.0],
        "ma5": [10.2],
        "signal_limit_up": [False],
    })


def test_daily_latest_returns_only_current_memory_row() -> None:
    client, repo = _client(_live_frame(), date.today())

    response = client.get("/api/kline/daily/latest", params={"symbol": "600000.SH"})

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "600000.SH"
    assert body["source"] == "live"
    assert body["row"] == {
        "symbol": "600000.SH",
        "date": date.today().isoformat(),
        "open": 10.0,
        "high": 10.8,
        "low": 9.9,
        "close": 10.6,
        "volume": 123_456.0,
        "amount": 1_234_560.0,
        "change_pct": None,
        "ma5": 10.2,
        "is_live": True,
    }
    assert repo.latest_asset_calls == 0


def test_daily_latest_returns_none_for_stale_cache() -> None:
    client, _ = _client(_live_frame(), date.today() - timedelta(days=1))

    response = client.get("/api/kline/daily/latest", params={"symbol": "600000.SH"})

    assert response.status_code == 200
    assert response.json() == {
        "symbol": "600000.SH",
        "row": None,
        "source": "none",
    }


def test_daily_latest_returns_none_when_symbol_is_missing() -> None:
    client, _ = _client(_live_frame(), date.today())

    response = client.get("/api/kline/daily/latest", params={"symbol": "600001.SH"})

    assert response.status_code == 200
    assert response.json() == {
        "symbol": "600001.SH",
        "row": None,
        "source": "none",
    }


def test_daily_latest_uses_etf_enriched_cache() -> None:
    etf = _live_frame().with_columns(pl.lit("510300.SH").alias("symbol"))
    client, repo = _client(
        pl.DataFrame(),
        None,
        asset_type="etf",
        latest_asset=(etf, date.today()),
    )

    response = client.get("/api/kline/daily/latest", params={"symbol": "510300.SH"})

    assert response.status_code == 200
    assert response.json()["row"]["close"] == 10.6
    assert response.json()["source"] == "live"
    assert repo.latest_asset_calls == 1
    assert repo.latest_asset_refresh is False


def test_daily_latest_uses_index_enriched_cache(monkeypatch) -> None:
    """指数日K的当日实时行走独立 index enriched 缓存, 不能直接 return None。"""
    from app.api import kline as kline_api

    monkeypatch.setattr(kline_api, "cn_today", lambda: date.today())
    idx = _live_frame().with_columns(pl.lit("000001.SH").alias("symbol"))
    client, repo = _client(
        pl.DataFrame(),
        None,
        asset_type="index",
        latest_asset=(idx, date.today()),
    )

    response = client.get("/api/kline/daily/latest", params={"symbol": "000001.SH"})

    assert response.status_code == 200
    body = response.json()
    assert body["row"] is not None
    assert body["row"]["close"] == 10.6
    assert body["source"] == "live"
    assert repo.latest_asset_calls == 1
    assert repo.latest_asset_refresh is False
