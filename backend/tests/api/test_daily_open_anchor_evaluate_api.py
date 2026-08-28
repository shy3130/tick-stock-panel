from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.data_providers.fquant.daily_market_research as dmr
from app.api.research import router


def _client(repo=None):
    app = FastAPI()
    app.state.repo = repo
    app.include_router(router)
    return TestClient(app)


def _canonical(manifest=None):
    return SimpleNamespace(
        generation=lambda: "canon-gen",
        manifest_sha256=lambda: "a" * 64,
        manifest=lambda: manifest if manifest is not None else {"source_generations": {"markets": {"generation": "mkt-gen", "manifest_sha256": "b" * 64}}},
        market_days=lambda start, end: [],
        daily_bars=lambda symbol, start, end: None,
        columns=lambda: ("date", "open", "high", "low", "close", "raw_open", "raw_high", "raw_low", "raw_close"),
    )


def test_capability_is_unavailable_without_canonical_reader():
    response = _client(None).get("/api/research/daily-open-anchor")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "canonical_reader_missing" in body["reasons"]


def test_capability_reports_markets_pin_opened(monkeypatch):
    class FakeReader:
        _column_names = {"code", "asset_type", "trade_date", "price", "ztj", "zrspj", "jrkpj", "zgj", "zdj", "zspj"}
        _has_payload_json = False
        _quote_columns = {"jrkpj": "jrkpj", "zgj": "zgj", "zdj": "zdj", "price": "price"}
        _direct_fields = {"price": True, "ztj": True}
        closed = False

        @classmethod
        def from_canonical_manifest(cls, manifest):
            return cls()

        def generation(self):
            return "mkt-gen"

        def pin_manifest_sha256(self):
            return "b" * 64

        def pin_identity_verified(self):
            return True

        def close(self):
            self.closed = True
    monkeypatch.setattr(dmr, "PublishedDailyMarketFactsReader", FakeReader)
    response = _client(SimpleNamespace(generation_pinned_daily_reader=_canonical())).get(
        "/api/research/daily-open-anchor"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["markets_facts"]["opened"] is True
    assert body["markets_facts"]["pin"] == "mkt-gen"


def test_evaluate_is_unavailable_without_canonical_reader():
    response = _client(None).post(
        "/api/research/factors/daily-open-anchor/evaluate",
        json={"start": "2026-01-01", "end": "2026-01-10", "oos_start": "2026-01-06", "symbols": ["600000.SH"]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"


def test_evaluate_rejects_invalid_date_range():
    response = _client(SimpleNamespace(generation_pinned_daily_reader=_canonical())).post(
        "/api/research/factors/daily-open-anchor/evaluate",
        json={"start": "2026-01-10", "end": "2026-01-01", "oos_start": "2026-01-05", "symbols": ["600000.SH"]},
    )
    assert response.status_code == 400
