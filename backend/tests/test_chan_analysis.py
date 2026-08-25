from __future__ import annotations

from datetime import date, timedelta
from math import sin
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import HTTPException

from app.api.indices import get_index_chan
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
