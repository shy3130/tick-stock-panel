from datetime import datetime
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import market_data


def _reset_market_data_state() -> None:
    for provider in market_data._provider_instances.values():
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    market_data._provider_instances.clear()
    market_data._status_cache.clear()


class Provider:
    name = "fake"

    def get_chip_distribution(self, symbol, start, end, limit):
        return pl.DataFrame({"symbol": [symbol], "trade_date": [start], "profit_ratio": [float("nan")]})

    def get_moneyflow_stock(self, symbol, start, end, freq):
        return pl.DataFrame()

    def get_moneyflow_blocks(self, trade_date, freq, block_type, limit):
        return pl.DataFrame()

    def get_call_auction(self, symbol, trade_date, session, limit):
        return pl.DataFrame({"symbol": [symbol], "trade_date": [trade_date]})

    def get_transactions(self, symbol, trade_date, limit):
        return pl.DataFrame()

    def get_market_data_status(self):
        return {"chip": {"available": True, "source": "fake"}}

    def close(self):
        pass


def client(monkeypatch, provider=None):
    app = FastAPI()
    app.include_router(market_data.router)
    monkeypatch.setattr(market_data, "get_provider", lambda name: provider or Provider())
    monkeypatch.setattr(market_data, "get_active_provider_name", lambda capability=None: "fake")
    _reset_market_data_state()
    return TestClient(app)


def test_symbol_and_range_validation(monkeypatch):
    c = client(monkeypatch)
    assert c.get("/api/market-data/chip/1.SZ?start=2020-01-01&end=2020-01-02").status_code == 422
    assert c.get("/api/market-data/chip/000001.SZ?start=2020-01-01&end=2026-01-02").status_code == 422
    assert c.get("/api/market-data/moneyflow/stock/000001.SZ?freq=minute&start=2024-01-01&end=2024-01-02").status_code == 422
    assert c.get("/api/market-data/chip/00700.HK?start=2024-01-01&end=2024-01-02").status_code == 422
    assert c.get("/api/market-data/call-auction/00700.HK?date=2024-01-02").status_code == 422
    assert c.get("/api/market-data/transactions/00700.HK?date=2024-01-02").status_code == 422


def test_empty_rows_and_json_safe(monkeypatch):
    c = client(monkeypatch)
    out = c.get("/api/market-data/moneyflow/stock/000001.SZ?start=2024-01-01&end=2024-01-02")
    assert out.status_code == 200 and out.json()["rows"] == []
    out = c.get("/api/market-data/chip/000001.SZ?start=2024-01-01&end=2024-01-02")
    assert out.status_code == 200 and out.json()["rows"][0]["profit_ratio"] is None


def test_microstructure_and_block_type_validation(monkeypatch):
    c = client(monkeypatch)
    assert c.get("/api/market-data/call-auction/000001.SZ?date=2024-01-02&session=morning").status_code == 422
    assert c.get("/api/market-data/moneyflow/blocks?date=2024-01-02&block_type=industry").status_code == 422
    assert c.get("/api/market-data/moneyflow/blocks?date=2024-01-02&block_type=0").status_code == 422
    assert c.get("/api/market-data/moneyflow/blocks?date=2024-01-02&block_type=43").status_code == 422


def test_transaction_contract_passes_datetime_to_provider(monkeypatch):
    seen = {}

    class RecordingProvider(Provider):
        def get_microstructure_status(self):
            return {"transactions": {"available": True, "source": "engine-a"}}

        def get_transactions(self, symbol, trade_date, limit):
            seen.update(symbol=symbol, trade_date=trade_date, limit=limit)
            return pl.DataFrame(
                {
                    "symbol": [symbol],
                    "datetime": ["2024-01-02 09:30:00"],
                    "price": [10.0],
                    "volume": [100.0],
                    "amount": [1_000.0],
                    "order_count": [None],
                    "direction": [1],
                    "venue": [None],
                    "source": ["fake:engine-a"],
                }
            )

    out = client(monkeypatch, RecordingProvider()).get(
        "/api/market-data/transactions/000001.SZ?date=2024-01-02&limit=10"
    )

    assert out.status_code == 200
    assert seen == {
        "symbol": "000001.SZ",
        "trade_date": datetime(2024, 1, 2),
        "limit": 10,
    }
    assert out.json()["rows"][0]["order_count"] is None


def test_missing_provider_method_is_503(monkeypatch):
    c = client(monkeypatch, SimpleNamespace(name="fake"))
    out = c.get("/api/market-data/chip/000001.SZ?start=2024-01-01&end=2024-01-02")
    assert out.status_code == 503 and "get_chip_distribution" in out.json()["detail"]


def test_status_fills_all_capabilities(monkeypatch):
    out = client(monkeypatch).get("/api/market-data/status")
    body = out.json()
    assert out.status_code == 200
    assert set(body["capabilities"]) == set(market_data._CAPABILITY_KEYS)
    assert body["capabilities"]["hk_financial"]["available"] is False


def test_status_coverage_is_cached(monkeypatch):
    calls = 0

    class CountingProvider(Provider):
        def get_market_data_status(self):
            nonlocal calls
            calls += 1
            return super().get_market_data_status()

    c = client(monkeypatch, CountingProvider())

    assert c.get("/api/market-data/status").status_code == 200
    assert c.get("/api/market-data/status").status_code == 200
    assert calls == 1
