from datetime import date
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl

from app.api import screener


def test_realtime_limit_frame_computes_limit_up_streak_broken_and_limit_down() -> None:
    previous = pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH", "600003.SH", "600004.SH"],
        "name": ["连板", "炸板", "跌停", "无实时"],
        "close": [10.0, 10.0, 10.0, 10.0],
        "consecutive_limit_ups": [2, 0, 0, 1],
        "consecutive_limit_downs": [0, 0, 1, 0],
    })
    quotes = [
        {"symbol": "600001.SH", "last_price": 11.0, "open": 10.2, "high": 11.0, "low": 10.1, "volume": 100, "amount": 1_100},
        {"symbol": "600002.SH", "last_price": 10.6, "open": 10.2, "high": 11.0, "low": 10.1, "volume": 200, "amount": 2_100},
        {"symbol": "600003.SH", "last_price": 9.0, "open": 9.4, "high": 9.5, "low": 9.0, "volume": 300, "amount": 2_800},
    ]

    result = screener._overlay_live_limit_signals(previous, quotes, date(2026, 7, 21))
    rows = {row["symbol"]: row for row in result.to_dicts()}

    assert set(rows) == {"600001.SH", "600002.SH", "600003.SH"}
    assert rows["600001.SH"]["signal_limit_up"] is True
    assert rows["600001.SH"]["consecutive_limit_ups"] == 3
    assert rows["600002.SH"]["signal_broken_limit_up"] is True
    assert rows["600002.SH"]["signal_limit_up"] is False
    assert rows["600003.SH"]["signal_limit_down"] is True
    assert rows["600003.SH"]["consecutive_limit_downs"] == 2
    assert all(row["date"] == date(2026, 7, 21) for row in rows.values())


def test_realtime_limit_frame_uses_provider_change_when_available() -> None:
    previous = pl.DataFrame({
        "symbol": ["600001.SH"],
        "name": ["示例"],
        "close": [10.0],
        "consecutive_limit_ups": [0],
        "consecutive_limit_downs": [0],
    })
    quotes = [{
        "symbol": "600001.SH", "last_price": 10.5, "prev_close": 10.0,
        "open": 10.1, "high": 10.6, "low": 10.0, "change_pct": 0.05,
    }]

    row = screener._overlay_live_limit_signals(previous, quotes, date(2026, 7, 21)).to_dicts()[0]

    assert row["change_pct"] == 0.05


def test_limit_ladder_default_uses_realtime_date_and_history_skips_provider(monkeypatch) -> None:
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    previous = pl.DataFrame({
        "symbol": ["600001.SH"], "name": ["连板"], "close": [10.0],
        "consecutive_limit_ups": [2], "consecutive_limit_downs": [0],
        "signal_limit_up": [True], "signal_limit_down": [False],
        "signal_broken_limit_up": [False], "signal_limit_down_recovery": [False],
    })

    class Service:
        def __init__(self, repo):
            pass

        def latest_date(self):
            return date(2026, 7, 20)

        def _load_enriched_for_date(self, as_of):
            return previous if as_of == date(2026, 7, 20) else pl.DataFrame()

        def load_prior_consecutive(self, as_of, column):
            return pl.DataFrame()

    timestamp = int(
        datetime(2026, 7, 21, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
    )
    calls = []

    class Provider:
        def get_realtime(self, universes=None, symbols=None):
            calls.append(symbols)
            return [{
                "symbol": "600001.SH", "last_price": 11.0, "open": 10.2,
                "high": 11.0, "low": 10.1, "volume": 100, "amount": 1_100,
                "timestamp": timestamp,
            }]

    monkeypatch.setattr(screener, "ScreenerService", Service)
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "clickhouse")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: Provider())
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repo=object(), depth_service=None))
    )

    live = screener.limit_ladder(request, direction="up", ext_columns=None)
    historical = screener.limit_ladder(
        request, as_of=date(2026, 7, 20), direction="up", ext_columns=None
    )

    assert live["as_of"] == "2026-07-21"
    assert live["is_realtime"] is True
    assert live["tiers"][0]["boards"] == 3
    assert calls == [None]
    assert historical["as_of"] == "2026-07-20"
