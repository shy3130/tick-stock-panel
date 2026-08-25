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
                "symbol": "000001.INDEX",
                "name": "上证指数",
                "last_price": 4043.64,
                "prev_close": 4028.90,
                "source": "tencent",
            }
        ])


def _client(repo, quote_service=None):
    app = FastAPI()
    app.include_router(intraday_api.router)
    app.state.repo = repo
    app.state.quote_service = quote_service
    return TestClient(app)


def test_index_quotes_use_provider_realtime_before_daily_fallback(monkeypatch):
    class Repo:
        def execute_all(self, query, params):  # noqa: ARG002
            raise AssertionError("daily fallback should not be used when provider returns realtime rows")

    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: FakeProvider())
    monkeypatch.setattr(intraday_api, "_maybe_external_fallback", lambda _symbols, rows: (rows, {}))

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
            return [("000001.INDEX", "2026-07-02", 4028.9, 4112.5)]

    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: EmptyProvider())
    monkeypatch.setattr(intraday_api, "_maybe_external_fallback", lambda _symbols, rows: (rows, {}))

    resp = _client(Repo()).get("/api/intraday/indices?symbols=000001.SH")

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "index_daily"
    assert body["rows"][0]["date"] == "2026-07-02"



def test_index_quotes_convert_provider_non_finite_numbers_to_null(monkeypatch):
    class NonFiniteProvider:
        capabilities = SimpleNamespace(realtime=True)

        def get_realtime(self, symbols=None, universes=None):  # noqa: ARG002
            return pl.DataFrame([{
                "symbol": "000001.INDEX",
                "last_price": float("inf"),
                "prev_close": float("nan"),
                "change_pct": -float("inf"),
            }])

    class Repo:
        def execute_all(self, query, params):  # noqa: ARG002
            raise AssertionError("provider row should be returned without daily fallback")

    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: NonFiniteProvider())
    monkeypatch.setattr(intraday_api, "_maybe_external_fallback", lambda _symbols, rows: (rows, {}))

    response = _client(Repo()).get("/api/intraday/indices?symbols=000001.SH")
    assert response.status_code == 200
    row = response.json()["rows"][0]
    assert row["last_price"] is None
    assert row["prev_close"] is None
    assert row["change_pct"] is None


def test_indices_endpoint_canonicalizes_sh_to_index(monkeypatch):
    """旧式 .SH 入参经 /indices 端点规范化为 .INDEX — provider 收到 canonical symbol。"""
    captured: list = []

    class CapturingProvider:
        capabilities = SimpleNamespace(realtime=True)

        def get_realtime(self, symbols=None, universes=None):  # noqa: ARG002
            captured.append(symbols)
            return pl.DataFrame([{
                "symbol": "000001.INDEX", "name": "上证指数",
                "last_price": 3966.594, "prev_close": 3950.0,
            }])

    monkeypatch.setattr("app.data_providers.registry.get_active_provider_name", lambda capability=None: "fquant_local")
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: CapturingProvider())
    monkeypatch.setattr(intraday_api, "_maybe_external_fallback", lambda _symbols, rows: (rows, {}))

    resp = _client(type("R", (), {"execute_all": lambda self, q, p: []})()).get(
        "/api/intraday/indices?symbols=000001.SH"
    )
    assert resp.status_code == 200
    # provider fallback 收到 canonical .INDEX, 不传旧 .SH
    assert captured and captured[0] == ["000001.INDEX"]
    assert resp.json()["rows"][0]["symbol"] == "000001.INDEX"


def test_snapshot_without_symbols_preserves_default_index_snapshot(monkeypatch):
    class QuoteService:
        def get_index_quotes(self, symbols=None):
            assert symbols is None
            return pl.DataFrame([
                {"symbol": "000001.INDEX", "last_price": 3966.59},
                {"symbol": "399001.INDEX", "last_price": 14317.0},
            ])

    monkeypatch.setattr(
        intraday_api,
        "_maybe_external_fallback",
        lambda _symbols, rows: (rows, {}),
    )

    response = _client(object(), QuoteService()).get("/api/intraday/snapshot")

    assert response.status_code == 200
    assert response.json()["source"] == "realtime"
    assert [row["symbol"] for row in response.json()["rows"]] == [
        "000001.INDEX",
        "399001.INDEX",
    ]


def test_snapshot_classifies_sh_index_but_sz_stock(monkeypatch):
    """000001.SH = 指数, 000001.SZ = 平安银行 (股票) — 后缀区分不误判。"""
    from app.api.intraday import _is_index_symbol

    assert _is_index_symbol("000001.SH") is True
    assert _is_index_symbol("000001.INDEX") is True
    assert _is_index_symbol("000001.SZ") is False   # 平安银行
    assert _is_index_symbol("600519.SH") is False    # 茅台
    assert _is_index_symbol("399001.SZ") is True     # 深证成指
    assert _is_index_symbol("399001.INDEX") is True