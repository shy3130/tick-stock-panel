from __future__ import annotations

from datetime import date

import polars as pl

from app.backtest.matrix import build_market_data_matrix
from app.strategy.builtin import high_turnover_surge


def test_high_turnover_surge_uses_percent_value_turnover_rate():
    panel = pl.DataFrame({
        "symbol": ["low", "hit", "low", "hit"],
        "date": [date(2024, 1, 2), date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 3)],
        "open": [100.0, 100.0, 104.0, 104.0],
        "high": [100.0, 100.0, 104.0, 104.0],
        "low": [100.0, 100.0, 104.0, 104.0],
        "close": [100.0, 100.0, 104.0, 104.0],
        "volume": [1000.0, 1000.0, 1000.0, 1000.0],
        "turnover_rate": [4.9, 5.1, 4.9, 5.1],
    })
    market = build_market_data_matrix(panel, field_columns={"turnover_rate"})
    signals = high_turnover_surge.MATRIX_STRATEGY.compute_signals(
        market,
        {"min_turnover": 5.0, "min_change": 3.0},
    )

    selected = [
        symbol
        for symbol, hit in zip(market.symbols, signals.entry[-1], strict=True)
        if hit
    ]
    assert selected == ["hit"]
