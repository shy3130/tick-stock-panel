from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_market_data_matrix
from app.backtest.strategy import StrategyBacktestService


def test_breadth_hysteresis_is_lagged_and_avoids_threshold_churn():
    breadth20 = np.array([0.60, 0.52, 0.50, 0.44, 0.70], dtype=np.float64)
    breadth60 = np.array([0.55, 0.48, 0.45, 0.42, 0.65], dtype=np.float64)
    allow = StrategyBacktestService._breadth_hysteresis_allow(
        breadth20,
        breadth60,
        {
            "enter_ma20": 0.55,
            "enter_ma60": 0.50,
            "exit_ma20": 0.45,
            "exit_ma60": 0.40,
        },
    )
    # Day 0 has no prior close. Day 1 enters from day 0; the middle band keeps
    # state until day 3 breadth is observed on day 4.
    np.testing.assert_array_equal(allow, [False, True, True, True, False])


def test_breadth_hysteresis_rejects_inverted_or_nonfinite_thresholds():
    values = np.array([0.5, 0.6], dtype=np.float64)
    with pytest.raises(ValueError, match="enter thresholds"):
        StrategyBacktestService._breadth_hysteresis_allow(
            values,
            values,
            {"enter_ma20": 0.4, "exit_ma20": 0.5},
        )
    with pytest.raises(ValueError, match="finite values"):
        StrategyBacktestService._breadth_hysteresis_allow(
            values,
            values,
            {"enter_ma20": float("nan")},
        )


def test_breadth_hysteresis_treats_missing_observation_as_bearish():
    breadth20 = np.array([0.60, np.nan, 0.70], dtype=np.float64)
    breadth60 = np.array([0.60, np.nan, 0.70], dtype=np.float64)
    allow = StrategyBacktestService._breadth_hysteresis_allow(
        breadth20,
        breadth60,
        {},
    )
    np.testing.assert_array_equal(allow, [False, True, False])


def test_market_breadth_uses_cross_sectional_ma_features():
    rows = []
    start = date(2025, 1, 1)
    for offset in range(90):
        for asset_id, symbol in enumerate(("000001.SZ", "000002.SZ", "600000.SH")):
            slope = 0.05 if asset_id < 2 else -0.02
            close = 10.0 + asset_id + slope * offset
            rows.append({
                "symbol": symbol,
                "date": start + timedelta(days=offset),
                "open": close - 0.02,
                "high": close + 0.10,
                "low": close - 0.10,
                "close": close,
                "volume": 1000.0,
            })
    market = build_market_data_matrix(pl.DataFrame(rows))
    allow = StrategyBacktestService._market_breadth_allow_array(
        market,
        {
            "enter_ma20": 0.50,
            "enter_ma60": 0.50,
            "exit_ma20": 0.30,
            "exit_ma60": 0.30,
            "min_valid_assets": 3,
        },
    )
    assert allow.dtype == np.bool_
    assert allow.shape == (90,)
    assert bool(allow[-1]) is True


def test_market_structure_cache_is_aligned_without_second_lag(tmp_path):
    path = tmp_path / "market_structure_v1.parquet"
    pl.DataFrame(
        {
            "date": [
                date(2025, 1, 1),
                date(2025, 1, 2),
                date(2025, 1, 3),
            ],
            "regime": ["warmup", "structural_bull", "structural_bear"],
            "protocol_hash": ["abc", "abc", "abc"],
        }
    ).write_parquet(path)

    allow = StrategyBacktestService._market_structure_cache_allow_array(
        ("2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04"),
        {
            "type": "market_structure_v1",
            "parquet": str(path),
            "protocol_hash": "abc",
        },
    )

    np.testing.assert_array_equal(allow, [False, True, False, False])


def test_market_structure_cache_rejects_protocol_mismatch(tmp_path):
    path = tmp_path / "market_structure_v1.parquet"
    pl.DataFrame(
        {
            "date": [date(2025, 1, 1)],
            "regime": ["structural_bull"],
            "protocol_hash": ["actual"],
        }
    ).write_parquet(path)

    with pytest.raises(ValueError, match="protocol hash mismatch"):
        StrategyBacktestService._market_structure_cache_allow_array(
            ("2025-01-01",),
            {
                "type": "market_structure_v1",
                "parquet": str(path),
                "protocol_hash": "expected",
            },
        )
