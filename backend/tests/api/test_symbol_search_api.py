from types import SimpleNamespace

import polars as pl

from app.api import kline


class Repo:
    def get_instruments(self):
        return pl.DataFrame([{"symbol": "600519.SH", "code": "600519", "name": "贵州茅台"}])

    def get_index_instruments(self):
        return pl.DataFrame()

    def get_etf_instruments(self):
        return pl.DataFrame()


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
