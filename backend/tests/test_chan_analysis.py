from __future__ import annotations

from datetime import date, datetime, timedelta
from math import sin
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import HTTPException

from app.api.indices import get_index_chan, get_index_chan_minute
from app.services import chan_analysis


def _sample(rows: int = 1200) -> pl.DataFrame:
    records = []
    for index in range(rows):
        close = 100 + 12 * sin(index / 9) + 2 * sin(index / 3)
        open_ = close + sin(index)
        records.append(
            {
                "date": date(2022, 1, 1) + timedelta(days=index),
                "open": open_,
                "high": max(open_, close) + 1,
                "low": min(open_, close) - 1,
                "close": close,
                "volume": 1000 + index,
            }
        )
    return pl.DataFrame(records)


def _minute_sample(days: int = 2) -> pl.DataFrame:
    records = []
    for day in range(days):
        trade_day = date(2026, 8, 20) + timedelta(days=day)
        times = [datetime.combine(trade_day, datetime.min.time()) + timedelta(hours=9, minutes=30 + minute) for minute in range(121)]
        times += [datetime.combine(trade_day, datetime.min.time()) + timedelta(hours=13, minutes=minute) for minute in range(1, 121)]
        for index, dt in enumerate(times):
            value = 100 + day + index / 100
            records.append({
                "symbol": "000001.SH", "datetime": dt,
                "open": value, "high": value + 1, "low": value - 1, "close": value + 0.5,
                "volume": 10.0, "amount": 1000.0,
            })
    return pl.DataFrame(records)


def test_analyze_levels_fallback_builds_linked_entities(monkeypatch):
    monkeypatch.setattr(chan_analysis, "_czsc", None)

    result = chan_analysis.analyze_levels(_sample(), "000001.SH")

    assert result["engine"] == "builtin"
    assert [level["key"] for level in result["levels"]] == ["daily", "weekly", "monthly"]
    assert len(result["levels"][0]["bars"]) == 1200
    assert len(result["levels"][0]["bars"]) > len(result["levels"][1]["bars"]) > len(result["levels"][2]["bars"])
    for level in result["levels"]:
        dates = {row["date"] for row in level["bars"]}
        assert all(pen["start"] in dates and pen["end"] in dates for pen in level["pens"])
        assert all(center["lower"] < center["upper"] for center in level["centers"])


def test_analyze_levels_rejects_missing_ohlc():
    with pytest.raises(ValueError, match="high"):
        chan_analysis.analyze_levels(pl.DataFrame({"date": [date.today()], "open": [1], "low": [1], "close": [1]}), "X")


@pytest.mark.skipif(chan_analysis._czsc is None, reason="czsc extra 未安装")
def test_analyze_levels_uses_czsc_when_installed():
    result = chan_analysis.analyze_levels(_sample(500), "000001.SH")

    assert result["engine"].startswith("czsc-")
    assert result["levels"][0]["pens"]


def test_index_chan_api_uses_index_repository(monkeypatch):
    monkeypatch.setattr(chan_analysis, "_czsc", None)
    repo = SimpleNamespace(get_index_daily=lambda symbol, start, end: _sample(120))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))

    result = get_index_chan(request, "000001.SH", date(2024, 1, 1), date(2024, 12, 31))

    assert result["symbol"] == "000001.SH"
    assert result["levels"][0]["bars"]


def test_index_chan_api_rejects_reversed_range():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=None)))

    with pytest.raises(HTTPException, match="start_date"):
        get_index_chan(request, "000001.SH", date(2025, 1, 2), date(2025, 1, 1))


def test_resample_minute_respects_cn_sessions():
    result = chan_analysis.resample_minute(_minute_sample(1), 120)

    assert result.height == 2
    assert result["datetime"].to_list() == [
        datetime(2026, 8, 20, 11, 30),
        datetime(2026, 8, 20, 15, 0),
    ]
    assert result["volume"].to_list() == [1210.0, 1200.0]


def test_index_chan_minute_uses_nearest_divisible_level(monkeypatch):
    monkeypatch.setattr(chan_analysis, "_czsc", None)
    one_minute = _minute_sample()
    five_minute = chan_analysis.resample_minute(one_minute, 5)

    def fake_fetch(symbol, start_time, end_time, period, asset_type):
        assert symbol == "000001.SH"
        assert asset_type == "index"
        return {1: one_minute, 5: five_minute}.get(period, pl.DataFrame())

    monkeypatch.setattr("app.services.kline_sync.fetch_minute_period", fake_fetch)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    result = get_index_chan_minute(request, "000001.SH", 45)

    assert [level["key"] for level in result["levels"]] == ["1f", "5f", "10f", "15f", "30f", "60f", "120f"]
    assert [(level["source"], level["source_period"]) for level in result["levels"]] == [
        ("direct", "1F"), ("direct", "5F"), ("synthetic", "5F"),
        ("synthetic", "5F"), ("synthetic", "15F"),
        ("synthetic", "30F"), ("synthetic", "60F"),
    ]
    assert result["levels"][-1]["bars"][-2]["date"].endswith("11:30")
    assert result["levels"][-1]["bars"][-1]["date"].endswith("15:00")
