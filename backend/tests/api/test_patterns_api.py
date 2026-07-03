from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl

from app.api import patterns


class Repo:
    def __init__(self, df: pl.DataFrame):
        self.df = df

    def get_daily(self, symbol, start, end, columns):  # noqa: ARG002
        return self.df.select(columns) if not self.df.is_empty() else self.df


def test_patterns_api_returns_empty_for_missing_data():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=Repo(pl.DataFrame()))))

    out = patterns.get_patterns("600519.SH", request)

    assert out == {"symbol": "600519.SH", "as_of": None, "patterns": []}


def test_patterns_api_uses_repo_daily():
    df = pl.DataFrame([
        {"date": date(2026, 1, 1) + timedelta(days=i), "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0, "volume": 1000}
        for i in range(20)
    ])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=Repo(df))))

    out = patterns.get_patterns("600519.SH", request, lookback=20)

    assert out["as_of"] == "2026-01-20"
    assert out["patterns"][0]["pattern"] == "consolidation"
