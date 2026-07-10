from __future__ import annotations

import polars as pl
import pytest

from app.indicators import pipeline
from app.services.quote_service import QuoteService


def _today_rows(turnover_rate: float | None = None) -> pl.DataFrame:
    row = {
        "symbol": "600000.SH",
        "open": 10.0,
        "high": 10.0,
        "low": 10.0,
        "close": 10.0,
        "raw_close": 10.0,
        "raw_high": 10.0,
        "volume": 8000.0,
    }
    if turnover_rate is not None:
        row["turnover_rate"] = turnover_rate
    return pl.DataFrame([row])


def _instruments() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH"],
        "name": ["Test"],
        "float_shares": [100_000_000.0],
    })


def test_quote_extra_normalizes_realtime_turnover_fraction_to_percent_value():
    out = QuoteService._build_quote_extra([{"symbol": "600000.SH", "turnover_rate": 0.008}])

    assert out["turnover_rate"][0] == pytest.approx(0.8)


def test_realtime_turnover_rate_uses_api_value_directly_after_entry_normalization():
    out = pipeline._compute_limit_signals_today(_today_rows(0.8), _instruments())

    assert out["turnover_rate"][0] == pytest.approx(0.8)


def test_realtime_turnover_rate_falls_back_to_float_shares_when_missing():
    out = pipeline._compute_limit_signals_today(_today_rows(), _instruments())

    assert out["turnover_rate"][0] == pytest.approx(0.8)
