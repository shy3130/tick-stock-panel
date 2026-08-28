from datetime import date, timedelta

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.research import router


class Reader:
    def has_columns(self, *columns): return True
    def manifest(self): return {"source_generations": {"markets": "m1"}}
    def generation(self): return "c1"
    def manifest_sha256(self): return "c" * 64
    def daily_bars(self, symbol, start, end):
        d = date(2024, 1, 1)
        return pl.DataFrame([{ "symbol": symbol, "date": d + timedelta(days=i), "open": 10., "high": 10.2, "low": 9.8, "close": 10., "raw_open": 10., "raw_high": 10.2, "raw_low": 9.8, "raw_close": 10.} for i in range(130)])
    def market_days(self, start, end): return []

class Facts:
    def get(self, symbol, day): return {"published_limit_up": 99., "suspended": False}
    def manifest_sha256(self): return "m" * 64

class Repo:
    generation_pinned_daily_reader = Reader()
    generation_pinned_market_facts_reader = Facts()


def client(repo=Repo()):
    app = FastAPI()
    app.state.repo = repo
    app.include_router(router)
    return TestClient(app)


def test_capability_endpoint():
    response = client().get("/api/research/zuoyi-defense")
    assert response.status_code == 200
    assert response.json()["definition"]["definition_version"] == "v3"


def test_invalid_request_returns_400():
    base = {"start": "2024-01-01", "end": "2024-04-01", "symbols": ["000001.SZ"], "oos_start": "2024-02-01"}
    extra = client().post("/api/research/factors/zuoyi-defense/evaluate", json={**base, "unknown": 1})
    assert extra.status_code == 400
    invalid_symbol = client().post("/api/research/factors/zuoyi-defense/evaluate", json={**base, "symbols": ["bad"]})
    assert invalid_symbol.status_code == 400
    invalid_cost = client().post("/api/research/factors/zuoyi-defense/evaluate", json={**base, "cost_bps": -1})
    assert invalid_cost.status_code == 400
