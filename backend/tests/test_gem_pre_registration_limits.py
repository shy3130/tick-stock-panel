"""创业板 2020-08-24 注册制改革前的涨跌幅为 ±10% (风险警示股 ±5%)。

改革当日 (2020-08-24) 起才是 ±20%; 科创板开板即 ±20%, 北交所不受影响。
标量 / Polars / NumPy 三条路径须给出同一结果, 否则 2020 年以前的回测
会把创业板 10% 涨停误判为未封板。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

from app.price_limits import (
    numpy_price_limit_matrix,
    polars_is_risk_warning_name,
    polars_price_limit_pct,
    price_limit_pct,
)

BEFORE = date(2020, 8, 21)
AFTER = date(2020, 8, 24)


@pytest.mark.parametrize(
    ("symbol", "trade_date", "is_st", "expected"),
    [
        ("300750.SZ", BEFORE, False, 0.10),
        ("300750.SZ", AFTER, False, 0.20),
        ("300001.SZ", BEFORE, True, 0.05),
        ("300001.SZ", AFTER, True, 0.20),
        ("301001.SZ", BEFORE, False, 0.10),
        ("688001.SH", BEFORE, False, 0.20),
        ("600001.SH", BEFORE, False, 0.10),
        ("830001.BJ", BEFORE, False, 0.30),
    ],
)
def test_scalar_gem_limit_before_registration(symbol, trade_date, is_st, expected):
    assert price_limit_pct(symbol, trade_date, is_risk_warning=is_st) == pytest.approx(expected)


def test_polars_and_numpy_gem_limit_before_registration():
    dates = [BEFORE, AFTER]
    symbols = ["300750.SZ", "300001.SZ", "688001.SH", "600001.SH"]
    names = ["宁德时代", "*ST创业", "科创", "主板"]
    panel = pl.DataFrame({
        "date": [value for value in dates for _ in symbols],
        "symbol": symbols * len(dates),
        "name": names * len(dates),
    }).with_columns(
        polars_is_risk_warning_name(pl.col("name")).alias("is_st")
    ).with_columns(
        polars_price_limit_pct(pl.col("symbol"), pl.col("date"), pl.col("is_st")).alias("pct")
    )
    expected = np.array([
        [0.10, 0.05, 0.20, 0.10],
        [0.20, 0.20, 0.20, 0.10],
    ])
    polars_values = panel["pct"].to_numpy().reshape(len(dates), len(symbols))
    np.testing.assert_allclose(polars_values, expected)
    np.testing.assert_allclose(numpy_price_limit_matrix(dates, symbols, names), expected, rtol=1e-6)


def test_backtest_lock_matrix_marks_pre_registration_gem_ten_percent_limit_up():
    from app.backtest.matrix import _limit_lock_matrices

    dates = [date(2020, 8, 20), BEFORE]
    close = np.array([[10.00], [11.00]], dtype=np.float32)
    seen = np.ones(close.shape, dtype=bool)
    nan = np.array([np.nan])
    up, down = _limit_lock_matrices(
        close, close.copy(), seen, dates, ["300750.SZ"], ["宁德时代"],
        {"limit_up": nan, "limit_down": nan},
        apply_latest_limits=False,
    )
    assert up[1, 0] == 1
    assert down[1, 0] == 0
