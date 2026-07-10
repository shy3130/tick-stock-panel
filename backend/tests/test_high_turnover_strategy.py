from __future__ import annotations

import polars as pl

from app.strategy.builtin import high_turnover_surge


def test_high_turnover_surge_uses_percent_value_turnover_rate():
    df = pl.DataFrame({
        "symbol": ["low", "hit"],
        "turnover_rate": [4.9, 5.1],
        "change_pct": [0.04, 0.04],
    })

    expr = high_turnover_surge.filter(df, {"min_turnover": 5.0, "min_change": 3.0})
    out = df.filter(expr)

    assert out["symbol"].to_list() == ["hit"]
