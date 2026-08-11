"""daily-batch 混合资产分组测试。"""
import datetime as _dt

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def test_daily_batch_groups_index_symbols(repo, monkeypatch):
    from app.api import kline as kline_api

    calls = {"stock_batch": [], "index": []}

    def fake_stock_batch(symbols, start, end, columns=None):
        calls["stock_batch"].append(list(symbols))
        return pl.DataFrame()

    def fake_index_daily(symbol, start, end, columns=None):
        calls["index"].append(symbol)
        return pl.DataFrame({
            "symbol": [symbol], "date": [_dt.date(2026, 7, 24)],
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1],
        })

    monkeypatch.setattr(repo, "get_daily_batch", fake_stock_batch)
    monkeypatch.setattr(repo, "get_index_daily", fake_index_daily)
    monkeypatch.setattr(repo, "get_index_symbol_set", lambda: {"000001.SH"})
    monkeypatch.setattr(repo, "get_etf_symbol_set", lambda: set())

    state = type("S", (), {"repo": repo})()
    req = type("R", (), {"app": type("A", (), {"state": state})()})()

    out = kline_api.get_daily_batch(req, {"symbols": ["600000.SH", "000001.SH"], "days": 12})
    assert calls["stock_batch"] == [["600000.SH"]]
    assert calls["index"] == ["000001.SH"]
    assert "000001.SH" in out["data"]
