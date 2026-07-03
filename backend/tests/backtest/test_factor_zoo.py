from datetime import date, timedelta

import numpy as np
import pandas as pd
import polars as pl

from app.backtest.factor_zoo import alpha101_001


def test_alpha101_001_matches_pandas_reference():
    start = date(2026, 1, 1)
    symbols = ["A", "B", "C"]
    rows = []
    for s_idx, sym in enumerate(symbols):
        for i in range(35):
            close = 10 + s_idx * 3 + i * (0.1 + s_idx * 0.03) + ((-1) ** i) * 0.05
            rows.append({"symbol": sym, "date": start + timedelta(days=i), "close": close})
    panel = pl.DataFrame(rows)

    actual = alpha101_001(panel).select("symbol", "date", "alpha101_001").sort(["date", "symbol"])
    expected = _alpha101_001_pandas(panel).sort(["date", "symbol"])
    joined = actual.join(expected, on=["symbol", "date"]).drop_nulls()

    assert joined.height > 0
    assert np.allclose(joined["alpha101_001"], joined["expected"], equal_nan=True)


def _alpha101_001_pandas(panel: pl.DataFrame) -> pl.DataFrame:
    pdf = panel.to_pandas()
    wide = pdf.pivot(index="date", columns="symbol", values="close").sort_index()
    returns = wide.pct_change()
    rolling_std = returns.rolling(20, min_periods=20).std(ddof=1)
    x = rolling_std.where(returns < 0, wide)
    powered = np.sign(x) * np.abs(x) ** 2
    argmax = powered.rolling(5, min_periods=5).apply(lambda arr: float(np.nanargmax(arr)), raw=True)
    ranked = argmax.rank(axis=1, method="average", pct=True, na_option="keep") - 0.5
    out = ranked.stack().reset_index(name="expected")
    return pl.from_pandas(out).with_columns(pl.col("date").cast(pl.Date))
