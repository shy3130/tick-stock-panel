from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.research import router


class _Reader:
    def __init__(self):
        self.days = [date(2026, 1, 1) + timedelta(days=index) for index in range(80)]
        rows = []
        for index, day in enumerate(self.days[:-1]):
            raw_open = 10.0 if index == 0 else 10.1
            raw_close = 10.3 if index == 0 else 10.2 + index * 0.02
            rows.append(
                {
                    "symbol": "600000.SH",
                    "date": day,
                    "raw_open": raw_open,
                    "raw_high": max(raw_open, raw_close) + 0.1,
                    "raw_low": 9.5 if index <= 5 else 9.6,
                    "raw_close": raw_close,
                }
            )
        self.frame = pl.DataFrame(rows)

    def generation(self):
        return "api-generation"

    def manifest_sha256(self):
        return "c" * 64

    def version(self):
        return "calendar-api-generation"

    def columns(self):
        return tuple(self.frame.columns)

    def market_days(self, start, end):
        return [day for day in self.days if start <= day <= end]

    def daily_bars(self, symbol, start, end):
        return self.frame.filter((pl.col("date") >= start) & (pl.col("date") <= end))


def _client() -> TestClient:
    app = FastAPI()
    app.state.repo = SimpleNamespace(generation_pinned_daily_reader=_Reader())
    app.include_router(router)
    return TestClient(app)


def test_macd_evaluate_route_uses_pinned_reader_and_oos_boundary():
    response = _client().post(
        "/api/research/factors/macd-stages/evaluate",
        json={
            "start": "2026-01-01",
            "end": "2026-03-10",
            "symbols": ["600000.SH"],
            "oos_start": "2026-02-10",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["provenance"]["pinned_reader"]["generation"] == "api-generation"
    assert body["segments"]["is"]["rows"]
    assert body["segments"]["oos"]["rows"]


def test_single_yang_evaluate_route_requires_native_raw_open_and_emits_event():
    client = _client()
    capability = client.get("/api/research/single-yang-no-break")
    assert capability.status_code == 200
    assert capability.json()["status"] == "available"

    response = client.post(
        "/api/research/factors/single-yang-no-break/evaluate",
        json={
            "start": "2026-01-01",
            "end": "2026-01-10",
            "symbols": ["600000.SH"],
            "oos_start": "2026-01-07",
            "cost_bps": 10,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["events"][0]["available_from"] == "2026-01-07"
    assert body["provenance"]["generation"] == "api-generation"


def test_n_shape_does_not_use_legacy_reader_attribute():
    client = _client()
    client.app.state.repo = SimpleNamespace(generation_pinned_daily_reader=_Reader())
    response = client.post(
        "/api/research/factors/n-shape/evaluate",
        json={"start": "2026-01-01", "end": "2026-01-10", "symbols": ["600000.SH"]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["unavailable_reasons"] == ["n_shape_research_reader_missing"]


class _ClosableNShapeReader:
    def __init__(self):
        self.closed = False

    def generation(self):
        return "canonical:test|markets:test"

    def manifest_sha256(self):
        return "a" * 64

    def provider_id(self):
        return "test"

    def source_provenance(self):
        return {
            "canonical": {"generation": "test", "manifest_sha256": "b" * 64},
            "markets": {"generation": "test", "manifest_sha256": "c" * 64},
        }

    def market_days(self, start, end):
        return [start + timedelta(days=index) for index in range((end - start).days + 1)]

    def universe(self, start, end):
        return []

    def daily_bars(self, symbol, start, end):
        raise AssertionError("empty universe must not read bars")

    def limit_regime_facts(self, symbol, start, end):
        raise AssertionError("empty universe must not read facts")

    def close(self):
        self.closed = True


def test_n_shape_route_closes_request_scoped_reader():
    client = _client()
    reader = _ClosableNShapeReader()
    client.app.state.repo = SimpleNamespace(n_shape_research_reader=reader)
    response = client.post(
        "/api/research/factors/n-shape/evaluate",
        json={"start": "2026-01-01", "end": "2026-01-10"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert reader.closed is True
