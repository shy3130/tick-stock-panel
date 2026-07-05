from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.intraday as intraday_api


class FakeProvider:
    capabilities = SimpleNamespace(realtime=True)

    def get_realtime(self, symbols=None, universes=None):  # noqa: ARG002
        return pl.DataFrame([
            {
                "symbol": "000001.SH",
                "name": "上证指数",
                "last_price": 4043.64,
                "prev_close": 4028.90,
                "source": "tencent",
            }
        ])


def _client(repo):
    app = FastAPI()
    app.include_router(intraday_api.router)
    app.state.repo = repo
    app.state.quote_service = None
    return TestClient(app)


def test_index_quotes_use_provider_realtime_before_daily_fallback(monkeypatch):
    class Repo:
        def execute_all(self, query, params):  # noqa: ARG002
            raise AssertionError("daily fallback should not be used when provider returns realtime rows")

    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: FakeProvider())

    resp = _client(Repo()).get("/api/intraday/indices?symbols=000001.SH")

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "provider_realtime"
    row = body["rows"][0]
    assert row["source"] == "tencent"
    assert row["last_price"] == 4043.64
    assert round(row["change_pct"], 4) == round((4043.64 - 4028.90) / 4028.90 * 100, 4)


def test_index_quotes_fallback_to_daily_when_provider_empty(monkeypatch):
    class EmptyProvider:
        capabilities = SimpleNamespace(realtime=True)

        def get_realtime(self, symbols=None, universes=None):  # noqa: ARG002
            return pl.DataFrame()

    class Repo:
        def execute_all(self, query, params):  # noqa: ARG002
            return [("000001.SH", "2026-07-02", 4028.9, 4112.5)]

    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: EmptyProvider())

    resp = _client(Repo()).get("/api/intraday/indices?symbols=000001.SH")

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "index_daily"
    assert body["rows"][0]["date"] == "2026-07-02"
