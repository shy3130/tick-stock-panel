from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import indices, kline


class Repo:
    def __init__(self, stock=None, index=None, etf=None, hk=None):
        self.stock = stock if stock is not None else pl.DataFrame(
            [{"symbol": "600519.SH", "code": "600519", "name": "贵州茅台"}],
        )
        self.index = index if index is not None else pl.DataFrame()
        self.etf = etf if etf is not None else pl.DataFrame()
        self.hk = hk if hk is not None else pl.DataFrame()

    def get_instruments(self):
        return self.stock

    def get_index_instruments(self):
        return self.index

    def get_etf_instruments(self):
        return self.etf

    def get_hk_instruments(self):
        return self.hk


def test_search_instruments_keeps_results_shape(monkeypatch):
    monkeypatch.setattr("app.services.symbol_search.suggest_symbols", lambda q, limit: [])
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=Repo())))

    out = kline.search_instruments(req, q="茅台", limit=10)

    assert out["results"][0]["symbol"] == "600519.SH"
    assert out["results"][0]["name"] == "贵州茅台"


def test_search_instruments_supports_pinyin(monkeypatch):
    monkeypatch.setattr("app.services.symbol_search.suggest_symbols", lambda q, limit: [])
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=Repo())))

    out = kline.search_instruments(req, q="gzmt", limit=10)

    assert out["results"][0]["symbol"] == "600519.SH"
    assert out["results"][0]["matched_by"] == "initials"



def test_search_instruments_filters_by_asset_type(monkeypatch):
    monkeypatch.setattr("app.services.symbol_search.suggest_symbols", lambda q, limit: [])
    repo = Repo(
        stock=pl.DataFrame([{"symbol": "600519.SH", "code": "600519", "name": "贵州茅台"}]),
        index=pl.DataFrame([{"symbol": "000001.INDEX", "code": "000001", "name": "上证指数"}]),
    )
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))

    out = kline.search_instruments(req, q="000001", limit=10, asset_type=["index"])

    assert [row["symbol"] for row in out["results"]] == ["000001.INDEX"]


def test_search_instruments_filters_index_only_query_parameters(monkeypatch):
    monkeypatch.setattr("app.services.symbol_search.suggest_symbols", lambda q, limit: [])
    app = FastAPI()
    app.state.repo = Repo(
        index=pl.DataFrame([{"symbol": "000001.INDEX", "code": "000001", "name": "上证指数"}]),
        etf=pl.DataFrame([{"symbol": "510210.SH", "code": "510210", "name": "上证指数ETF富国"}]),
    )
    app.include_router(kline.router)

    response = TestClient(app).get(
        "/api/kline/instruments/search",
        params={"q": "szzs", "asset_type": "index"},
    )

    assert response.status_code == 200
    assert [row["symbol"] for row in response.json()["results"]] == ["000001.INDEX"]

def test_index_only_search_never_calls_suggest(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.services.symbol_search.suggest_symbols",
        lambda q, limit: calls.append((q, limit)) or [{"symbol": "000001.SH", "asset_type": "index"}],
    )
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=Repo())))

    out = kline.search_instruments(req, q="不存在", limit=10, asset_type=["index"])

    assert out["results"] == []
    assert calls == []

def test_search_instruments_filters_hk_local_results(monkeypatch):
    monkeypatch.setattr("app.services.symbol_search.suggest_symbols", lambda q, limit: [])
    repo = Repo(hk=pl.DataFrame([{"symbol": "00700.HK", "code": "00700", "name": "腾讯控股"}]))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))

    out = kline.search_instruments(req, q="txkg", limit=10, asset_type=["hk"])

    assert [row["symbol"] for row in out["results"]] == ["00700.HK"]
    assert out["results"][0]["matched_by"] == "initials"


def test_search_instruments_supports_repeated_asset_type(monkeypatch):
    monkeypatch.setattr("app.services.symbol_search.suggest_symbols", lambda q, limit: [])
    repo = Repo(
        stock=pl.DataFrame([{"symbol": "000001.SZ", "code": "000001", "name": "平安银行"}]),
        index=pl.DataFrame([{"symbol": "000001.INDEX", "code": "000001", "name": "上证指数"}]),
    )
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))

    out = kline.search_instruments(req, q="000001", limit=10, asset_type=["stock", "index"])

    assert {row["symbol"] for row in out["results"]} == {"000001.SZ", "000001.INDEX"}


def test_search_instruments_accepts_repeated_asset_type_query_parameters(monkeypatch):
    monkeypatch.setattr("app.services.symbol_search.suggest_symbols", lambda q, limit: [])
    app = FastAPI()
    app.state.repo = Repo(
        stock=pl.DataFrame([{"symbol": "000001.SZ", "code": "000001", "name": "平安银行"}]),
        index=pl.DataFrame([{"symbol": "000001.INDEX", "code": "000001", "name": "上证指数"}]),
    )
    app.include_router(kline.router)

    response = TestClient(app).get(
        "/api/kline/instruments/search",
        params=[
            ("q", "000001"),
            ("asset_type", "stock"),
            ("asset_type", "stock"),
            ("asset_type", "index"),
        ],
    )

    assert response.status_code == 200
    assert {row["symbol"] for row in response.json()["results"]} == {"000001.SZ", "000001.INDEX"}


def test_search_indices_supports_pinyin_and_initials(monkeypatch):
    repo = Repo(index=pl.DataFrame([{"symbol": "000001.INDEX", "code": "000001", "name": "上证指数"}]))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))

    by_full = indices.search_indices(req, q="shangzheng", limit=10)
    by_initials = indices.search_indices(req, q="szzs", limit=10)

    assert by_full["results"][0]["symbol"] == "000001.INDEX"
    assert by_full["results"][0]["matched_by"] == "pinyin"
    assert by_initials["results"][0]["matched_by"] == "initials"

def test_search_indices_keeps_full_rows_and_100_result_limit():
    index_rows = [
        {
            "symbol": f"{number:06d}.INDEX",
            "code": f"{number:06d}",
            "name": "测试指数",
            "public_extra": f"extra-{number}",
        }
        for number in range(60)
    ]
    repo = Repo(index=pl.DataFrame(index_rows))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))

    out = indices.search_indices(req, q="测试指数", limit=100)

    assert len(out["results"]) == 60
    assert {row["public_extra"] for row in out["results"]} == {
        f"extra-{number}" for number in range(60)
    }