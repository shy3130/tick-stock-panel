import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl

from app.api import stock_analysis


class _Repo:
    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_daily_asset(self, asset_type, symbol, start, end):
        return pl.DataFrame({
            "symbol": [symbol],
            "date": [date(2026, 7, 20)],
            "open": [10.0],
            "high": [10.5],
            "low": [9.8],
            "close": [10.2],
            "volume": [1_000.0],
            "amount": [10_100.0],
        })


def _request() -> SimpleNamespace:
    state = SimpleNamespace(repo=_Repo(), quote_service=None)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_analysis_kline_merges_current_cn_realtime_quote(monkeypatch, tmp_path) -> None:
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    timestamp = int(
        datetime(2026, 7, 21, 14, 35, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
    )

    class RealtimeProvider:
        def get_realtime(self, universes=None, symbols=None):
            assert symbols == ["002842.SZ"]
            return [{
                "symbol": "002842.SZ",
                "last_price": 11.2,
                "prev_close": 10.2,
                "open": 10.3,
                "high": 11.5,
                "low": 10.1,
                "volume": 8_000.0,
                "amount": 88_000.0,
                "change_pct": None,
                "timestamp": timestamp,
            }]

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "clickhouse")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: RealtimeProvider())

    result = stock_analysis._load_analysis_kline(_request(), "002842.SZ", days=90)

    latest = result.frame.sort("date").tail(1).to_dicts()[0]
    assert latest["date"] == date(2026, 7, 21)
    assert latest["open"] == 10.3
    assert latest["high"] == 11.5
    assert latest["low"] == 10.1
    assert latest["close"] == 11.2
    assert latest["volume"] == 8_000.0
    assert latest["amount"] == 88_000.0
    assert result.is_realtime is True
    assert result.data_as_of == "2026-07-21"
    assert result.quote_timestamp == timestamp

    levels = stock_analysis.get_levels(_request(), "002842.SZ", days=90)
    assert levels["close"] == 11.2
    assert levels["data_as_of"] == "2026-07-21"
    assert levels["is_realtime"] is True
    assert levels["quote_timestamp"] == timestamp

    stream = stock_analysis.analyze_stock_stream(
        _Repo(),
        tmp_path,
        "002842.SZ",
        market="cn",
        kline_df=result.frame,
        data_as_of=result.data_as_of,
        is_realtime=result.is_realtime,
        quote_timestamp=result.quote_timestamp,
    )
    meta = json.loads(asyncio.run(anext(stream)))
    assert meta["close"] == 11.2
    assert meta["data_as_of"] == "2026-07-21"
    assert meta["is_realtime"] is True
    assert meta["quote_timestamp"] == timestamp
    asyncio.run(stream.aclose())


def test_analysis_kline_keeps_daily_data_without_valid_realtime(monkeypatch) -> None:
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    class RealtimeProvider:
        def get_realtime(self, universes=None, symbols=None):
            return []

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "clickhouse")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: RealtimeProvider())

    result = stock_analysis._load_analysis_kline(_request(), "002842.SZ", days=90)

    assert result.frame.tail(1)["date"][0] == date(2026, 7, 20)
    assert result.is_realtime is False
    assert result.data_as_of == "2026-07-20"
    assert result.quote_timestamp is None
