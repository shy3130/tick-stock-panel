from datetime import date, datetime

import numpy as np
import polars as pl
import pytest

from tivelo_market_cn.legacy import market_time as clock
from tivelo_market_cn.legacy import price_limits as limits


@pytest.mark.parametrize(
    "hour,minute,expected",
    [(9, 0, 0), (10, 0, 30), (11, 30, 120), (12, 0, 120), (14, 0, 180), (15, 1, 240)],
)
def test_legacy_clock_preserves_beijing_and_lunch_elapsed_minutes(hour, minute, expected):
    moment = datetime(2025, 1, 3, hour, minute, tzinfo=clock.CN_TZ)
    assert clock.trading_minutes_elapsed_from_dt(moment) == expected
    assert clock.trading_minutes_elapsed_from_ts(int(moment.timestamp() * 1000)) == expected


def test_legacy_clock_fallback_and_weekend_are_preserved():
    assert clock.trading_minutes_elapsed_from_ts(None) == 240
    assert clock.trading_minutes_elapsed_from_ts("invalid") == 240
    assert clock.trading_minutes_elapsed_from_dt(datetime(2025, 1, 4, 10)) == 240
    assert not clock.in_continuous_session(datetime(2025, 1, 4, 10))


def test_scalar_polars_numpy_compatibility_on_both_historical_boundaries():
    dates = [date(2020, 8, 21), date(2020, 8, 24), date(2026, 7, 3), date(2026, 7, 6)]
    symbols = ["600001.SH", "000001.SZ", "300001.SZ", "302001.SZ", "688001.SH", "830001.BJ"]
    names = ["*ST fixture"] * len(symbols)
    expected = np.array(
        [
            [0.05, 0.05, 0.05, 0.05, 0.20, 0.30],
            [0.05, 0.05, 0.20, 0.20, 0.20, 0.30],
            [0.05, 0.05, 0.20, 0.20, 0.20, 0.30],
            [0.10, 0.10, 0.20, 0.20, 0.20, 0.30],
        ]
    )
    scalar = [
        [limits.price_limit_pct(symbol, day, is_risk_warning=True) for symbol in symbols]
        for day in dates
    ]
    frame = pl.DataFrame(
        {"date": [d for d in dates for _ in symbols], "symbol": symbols * len(dates)}
    )
    vector = (
        frame.select(limits.polars_price_limit_pct(pl.col("symbol"), pl.col("date"), pl.lit(True)))
        .to_series()
        .to_numpy()
        .reshape(expected.shape)
    )
    for actual in (scalar, vector, limits.numpy_price_limit_matrix(dates, symbols, names)):
        np.testing.assert_allclose(actual, expected)


def test_legacy_rounding_and_validity_mask_stay_aligned():
    previous = np.array([18.90, 10.00])
    percentages = np.array([0.05, 0.10])
    actual = limits.numpy_limit_price(previous, percentages, up=False)
    frame = pl.DataFrame({"previous": previous, "limit": percentages})
    vector = (
        frame.select(limits.polars_limit_price(pl.col("previous"), pl.col("limit"), up=False))
        .to_series()
        .to_numpy()
    )
    np.testing.assert_array_equal(actual, vector)
    np.testing.assert_allclose(actual, [17.96, 9.00])
    target = np.empty((1, 2), dtype=np.float32)
    result = limits.write_numpy_price_limit_matrix(
        target,
        [date(2025, 1, 3)],
        ["600001.SH", "300001.SZ"],
        ["a", "b"],
        valid=np.array([[True, False]]),
    )
    assert result is target
    assert result[0, 0] == pytest.approx(0.10)
    assert np.isnan(result[0, 1])
