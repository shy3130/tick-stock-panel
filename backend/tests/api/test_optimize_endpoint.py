from datetime import date, timedelta

import numpy as np
import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.backtest import router


class _FakeRepo:
    def __init__(self, per_symbol: dict[str, list[int]]) -> None:
        self._per = per_symbol
        self.asset_type_calls: dict[str, str] = {}

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        self.asset_type_calls[symbol] = asset_type
        if symbol not in self._per:
            return pl.DataFrame()
        rng = np.random.default_rng(sum(bytearray(symbol.encode())))
        base = 10.0
        rows = []
        for offset in self._per[symbol]:
            base *= 1 + rng.normal(0, 0.01)
            rows.append({
                "symbol": symbol,
                "date": date.today() - timedelta(days=offset),
                "close": round(base, 3),
            })
        return pl.DataFrame(rows)

    def get_instruments(self):
        symbols = list(self._per.keys())
        return pl.DataFrame({"symbol": symbols, "name": [f"名{s[:3]}" for s in symbols]})


def _client(repo):
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    return TestClient(app)


def _full(n=60):
    return list(range(n))[::-1]


def test_optimize_risk_parity_weights_sum_to_one():
    repo = _FakeRepo({"000001.SZ": _full(), "000002.SZ": _full(), "600000.SH": _full()})

    resp = _client(repo).post(
        "/api/backtest/optimize",
        json={
            "symbols": ["000001.SZ", "000002.SZ", "600000.SH"],
            "method": "risk_parity",
            "lookback_days": 80,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["weights"]) == 3
    assert abs(sum(w["weight"] for w in body["weights"]) - 1.0) < 1e-6
    assert all(w["weight"] >= 0 for w in body["weights"])
    assert body["stats"]["n"] == 3


def test_optimize_score_weight_momentum_sums_to_one():
    repo = _FakeRepo({"000001.SZ": _full(), "000002.SZ": _full(), "600000.SH": _full()})

    resp = _client(repo).post(
        "/api/backtest/optimize",
        json={
            "symbols": ["000001.SZ", "000002.SZ", "600000.SH"],
            "method": "score_weight",
            "lookback_days": 80,
        },
    )

    assert resp.status_code == 200
    assert abs(sum(w["weight"] for w in resp.json()["weights"]) - 1.0) < 1e-5


def test_optimize_rejects_single_symbol():
    repo = _FakeRepo({"000001.SZ": _full()})

    resp = _client(repo).post("/api/backtest/optimize", json={"symbols": ["000001.SZ"], "method": "equal"})

    assert resp.status_code == 400


def test_optimize_etf_asset_type_and_hk_dropped():
    repo = _FakeRepo({"600519.SH": _full(), "513050.SH": _full()})

    resp = _client(repo).post(
        "/api/backtest/optimize",
        json={
            "symbols": ["600519.SH", "513050.SH", "00700.HK"],
            "method": "equal",
            "lookback_days": 80,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert repo.asset_type_calls["513050.SH"] == "etf"
    assert repo.asset_type_calls["00700.HK"] == "hk"
    assert "00700.HK" in body["meta"]["dropped"]
    assert set(body["meta"]["kept"]) == {"600519.SH", "513050.SH"}


def test_optimize_non_overlapping_dates_returns_400():
    repo = _FakeRepo({
        "000001.SZ": list(range(60, 30, -1)),
        "000002.SZ": list(range(30, 0, -1)),
    })

    resp = _client(repo).post(
        "/api/backtest/optimize",
        json={
            "symbols": ["000001.SZ", "000002.SZ"],
            "method": "risk_parity",
            "lookback_days": 80,
        },
    )

    assert resp.status_code == 400
