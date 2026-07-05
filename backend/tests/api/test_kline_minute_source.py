from datetime import date, datetime
from types import SimpleNamespace

import polars as pl

from app.api import kline


class FakeRepo:
    def execute_one(self, sql, params=None):
        return None

    def latest_minute_date(self, symbol):
        return None

    def latest_daily_date(self):
        return date(2026, 7, 3)

    def get_minute(self, symbol, trade_date):
        return pl.DataFrame()


def request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=FakeRepo())))


def test_minute_historical_uses_local_disk_provider(monkeypatch):
    calls = {"local": 0, "live": 0}

    class Provider:
        def get_minute(self, symbols, start_time, end_time, asset_type, freq="1m"):
            calls["local"] += 1
            return pl.DataFrame({
                "symbol": symbols,
                "datetime": ["2026-07-02 09:31:00"],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [100.0],
                "amount": [100.0],
            })

    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: Provider())
    monkeypatch.setattr(kline.kline_sync, "fetch_minute_single", lambda *a, **k: calls.__setitem__("live", calls["live"] + 1) or pl.DataFrame())

    resp = kline.get_minute(request(), "600519.SH", date(2026, 7, 2))

    assert resp["source"] == "local_disk"
    assert len(resp["rows"]) == 1
    assert calls == {"local": 1, "live": 0}


def test_minute_latest_intraday_uses_tdx_api(monkeypatch):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "code": 0,
                "data": {
                    "date": "20260703",
                    "List": [{"Time": "09:31", "Price": 1203000, "Number": 12}],
                },
            }

    seen = {}

    def fake_get(url, params, timeout, trust_env):
        seen.update({"url": url, "params": params, "trust_env": trust_env})
        return Resp()

    monkeypatch.setenv("FQUANT_TDX_API_BASE", "http://tdx.local")
    monkeypatch.setattr(kline, "_is_intraday_live_date", lambda trade_date, latest_date=None: True)
    monkeypatch.setattr(kline.httpx, "get", fake_get)

    resp = kline.get_minute(request(), "600519.SH", date(2026, 7, 3))

    assert resp["source"] == "tdx_api"
    assert resp["rows"][0]["datetime"] == "2026-07-03 09:31:00"
    assert resp["rows"][0]["close"] == 1203.0
    assert resp["rows"][0]["volume"] == 1200.0
    assert seen == {
        "url": "http://tdx.local/api/minute",
        "params": {"code": "600519", "date": "20260703"},
        "trust_env": False,
    }


def test_intraday_live_date_requires_today_and_session():
    assert kline._is_intraday_live_date(
        date(2026, 7, 3),
        date(2026, 7, 3),
        now=datetime(2026, 7, 3, 10, 0),
    )
    assert not kline._is_intraday_live_date(
        date(2026, 7, 2),
        date(2026, 7, 3),
        now=datetime(2026, 7, 3, 10, 0),
    )
    assert not kline._is_intraday_live_date(
        date(2026, 7, 3),
        date(2026, 7, 3),
        now=datetime(2026, 7, 3, 16, 0),
    )
