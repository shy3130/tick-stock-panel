from datetime import date
from types import SimpleNamespace

import polars as pl

from app.api import indices
from app.capabilities import Cap, CapabilityLimits, CapabilitySet


class FakeRepo:
    def get_index_instruments(self):
        return pl.DataFrame()

    def get_index_daily(self, symbol, start, end):
        return pl.DataFrame()


def request():
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=FakeRepo(),
                capabilities=CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(batch=500)}),
            )
        )
    )


def test_index_daily_fallback_fetches_index_asset_type(monkeypatch):
    calls = []

    def fake_sync(symbols, **kwargs):
        calls.append((symbols, kwargs))
        return pl.DataFrame({
            "symbol": symbols,
            "date": [date(2026, 7, 1)],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [4112.45],
            "volume": [1.0],
            "amount": [1.0],
        })

    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    monkeypatch.setattr(indices.kline_sync, "sync_daily_batch", fake_sync)
    monkeypatch.setattr(indices, "compute_enriched", lambda raw, **kwargs: raw)

    resp = indices.get_index_daily(
        request(),
        symbol="000001.SH",
        days=120,
        start_date="2026-07-01",
        end_date="2026-07-01",
    )

    assert resp["source"] == "live"
    assert resp["symbol"] == "000001.INDEX"  # 旧式 .SH 入参 → canonical .INDEX
    assert resp["rows"][0]["close"] == 4112.45
    assert calls[0][1]["asset_type"] == "index"
    assert calls[0][0] == ["000001.INDEX"]  # sync 收到 canonical symbol


def test_index_minute_fetches_index_asset_type(monkeypatch):
    calls = []

    def fake_fetch(symbol, trade_date, asset_type="stock"):
        calls.append((symbol, trade_date, asset_type))
        return pl.DataFrame({
            "symbol": [symbol],
            "datetime": ["2026-07-01 09:31:00"],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [1.0],
        })

    monkeypatch.setattr(indices.kline_sync, "fetch_minute_single", fake_fetch)

    resp = indices.get_index_minute(request(), symbol="000001.SH", trade_date=date(2026, 7, 1))

    assert resp["source"] == "live"
    assert resp["symbol"] == "000001.INDEX"  # canonical
    assert calls == [("000001.INDEX", date(2026, 7, 1), "index")]


def test_index_daily_accepts_canonical_index_suffix(monkeypatch):
    """纯 code 和 .INDEX 入参同样走指数路径，不猜普通股票上下文。"""
    captured = []

    def fake_sync(symbols, **kwargs):
        captured.append(symbols)
        return pl.DataFrame({"symbol": symbols, "date": [date(2026, 7, 1)],
                             "open": [1.0], "high": [1.0], "low": [1.0],
                             "close": [1.0], "volume": [1.0], "amount": [1.0]})

    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    monkeypatch.setattr(indices.kline_sync, "sync_daily_batch", fake_sync)
    monkeypatch.setattr(indices, "compute_enriched", lambda raw, **kwargs: raw)

    for inp in ("000001", "000001.INDEX", "000001.SH"):
        resp = indices.get_index_daily(request(), symbol=inp, days=120,
                                       start_date="2026-07-01", end_date="2026-07-01")
        assert resp["symbol"] == "000001.INDEX"
    assert all(s == ["000001.INDEX"] for s in captured)
