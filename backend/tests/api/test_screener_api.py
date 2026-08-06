from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import screener
from app.services import screener as screener_module


class _Service:
    def __init__(self, repo):
        self.repo = repo

    def latest_date(self):
        return date(2026, 7, 16)

    def _load_enriched_for_date(self, as_of):
        return pl.DataFrame(
            {
                "symbol": ["600001.SH", "000001.SZ"],
                "date": [as_of, as_of],
                "close": [10.0, 20.0],
                "change_pct": [0.1, 0.2],
            }
        )


class _Repo:
    store = SimpleNamespace(data_dir=Path("."))

    def get_instruments(self):
        return pl.DataFrame({"symbol": ["600001.SH", "000001.SZ"], "name": ["甲", "乙"]})


def _client(monkeypatch):
    monkeypatch.setattr(screener_module, "ScreenerService", _Service)
    app = FastAPI()
    app.state.repo = _Repo()
    app.include_router(screener.router)
    return TestClient(app)


def test_screener_query_fields_and_nested_order(monkeypatch):
    client = _client(monkeypatch)
    fields = client.get("/api/screener/fields")
    assert fields.status_code == 200
    assert any(item["field"] == "change_pct" for item in fields.json()["fields"])
    response = client.post(
        "/api/screener/query",
        json={
            "conditions": [{"field": "change_pct", "op": ">", "value": 0}],
            "order_by": {"field": "close", "direction": "asc"},
            "limit": 1,
        },
    )
    assert response.status_code == 200
    assert set(response.json()) == {"rows", "total", "applied", "as_of", "elapsed_ms"}
    assert response.json()["rows"][0]["symbol"] == "600001.SH"


def test_screener_query_distinguishes_422_400_and_503(monkeypatch):
    client = _client(monkeypatch)
    assert client.post("/api/screener/query", json={"conditions": []}).status_code == 422
    semantic = client.post(
        "/api/screener/query",
        json={"conditions": [{"field": "does_not_exist", "op": "=", "value": 1}]},
    )
    assert semantic.status_code == 400
    assert semantic.json()["detail"]["code"] == "invalid_screener_semantics"
    unavailable = client.post(
        "/api/screener/query",
        json={"conditions": [{"field": "main_net_inflow", "op": ">", "value": 0}]},
    )
    assert unavailable.status_code == 400
    assert unavailable.json()["detail"]["reason"] == "unavailable_field"


def test_nl_presets_shape_and_legacy_routes(monkeypatch):
    client = _client(monkeypatch)
    presets = client.get("/api/screener/nl_presets")
    assert presets.status_code == 200
    assert len(presets.json()["presets"]) == 5
    assert all(item["predicate"]["conditions"] for item in presets.json()["presets"])
    assert client.get("/api/screener/strategies").status_code == 200


def test_query_missing_required_source_is_sanitized_503(monkeypatch):
    class BrokenService(_Service):
        def _load_enriched_for_date(self, as_of):
            return pl.DataFrame({"symbol": ["600001.SH"], "date": [as_of]})

    monkeypatch.setattr(screener_module, "ScreenerService", BrokenService)
    app = FastAPI()
    app.state.repo = _Repo()
    app.include_router(screener.router)
    response = TestClient(app).post(
        "/api/screener/query",
        json={"conditions": [{"field": "close", "op": ">", "value": 0}]},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "screener_data_unavailable", "fields": ["change_pct", "close"]}}
    assert "path" not in response.text and "exception" not in response.text
