from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl
import pytest

from app.backtest.matrix import (
    MatrixComputeCache,
    MatrixPipelineConfig,
    MatrixStrategyPipeline,
    build_market_data_matrix,
    rolling_max,
    rolling_mean,
    rolling_min,
    rolling_quantile,
    rolling_std,
    rolling_sum,
    shift,
)
from app.strategy.engine import StrategyEngine

REPO_ROOT = Path(__file__).resolve().parents[3]


def _market(scale: float = 1.0):
    start = date(2024, 1, 1)
    rows = []
    for asset_id, symbol in enumerate(("000001.SZ", "600000.SH")):
        for time_id in range(140):
            close = scale * (10.0 + asset_id + time_id * 0.01)
            rows.append({
                "symbol": symbol,
                "name": symbol,
                "date": start + timedelta(days=time_id),
                "open": close * 0.99,
                "high": close * 1.01,
                "low": close * 0.98,
                "close": close,
                "volume": 100_000.0 + time_id,
                "amount": close * 100_000.0,
                "signal_limit_up": False,
                "signal_limit_down": False,
            })
    return build_market_data_matrix(pl.DataFrame(rows), field_columns={"amount"})


def test_cache_hits_are_read_only_and_temporary_arrays_use_content_fingerprint():
    market = _market()
    cache = MatrixComputeCache(max_bytes=8 * 1024 * 1024)

    with cache.activate(market):
        first = rolling_mean(market.close, 5)
        second = rolling_mean(market.close, 5)
        temp_first = rolling_mean(market.close * np.float32(2.0), 7)
        temp_second = rolling_mean(market.close * np.float32(2.0), 7)

    assert first is second
    assert temp_first is temp_second
    assert first.flags.writeable is False
    stats = cache.snapshot()
    assert stats["operations"]["rolling_mean"]["hits"] == 2
    assert stats["fingerprint_bytes"] > 0


def test_cache_key_separates_market_lineage_and_operator_parameters():
    first_market = _market()
    second_market = _market()
    cache = MatrixComputeCache(max_bytes=8 * 1024 * 1024)

    with cache.activate(first_market):
        first_window = rolling_min(first_market.close, 3)
        second_window = rolling_min(first_market.close, 4)
    with cache.activate(second_market):
        other_market = rolling_min(second_market.close, 3)

    assert first_window is not second_window
    assert first_window is not other_market
    assert cache.snapshot()["operations"]["rolling_min"]["misses"] == 3


def test_cache_lru_evicts_by_bytes_and_close_releases_all_entries():
    market = _market()
    item_bytes = market.close.nbytes
    cache = MatrixComputeCache(max_bytes=item_bytes, max_item_bytes=item_bytes)

    with cache.activate(market):
        rolling_min(market.close, 3)
        rolling_max(market.close, 3)

    before_close = cache.snapshot()
    assert before_close["entries"] == 1
    assert before_close["evictions"] == 1
    cache.close()
    assert cache.snapshot()["current_bytes"] == 0
    with pytest.raises(RuntimeError, match="closed"), cache.activate(market):
        pass


def test_shift_stays_out_of_cache_to_protect_expensive_working_set():
    market = _market()
    cache = MatrixComputeCache(max_bytes=8 * 1024 * 1024)

    with cache.activate(market):
        first = shift(market.close, 1)
        second = shift(market.close, 1)

    assert first is not second
    assert "shift" not in cache.snapshot()["operations"]


def test_additional_rolling_operators_match_pandas_and_hit_cache():
    market = _market()
    cache = MatrixComputeCache(max_bytes=8 * 1024 * 1024)
    expected = pd.DataFrame(market.close)

    with cache.activate(market):
        actual_sum = rolling_sum(market.close, 5)
        actual_std = rolling_std(market.close, 5)
        actual_quantile = rolling_quantile(market.close, 5, 0.25)
        assert rolling_sum(market.close, 5) is actual_sum
        assert rolling_std(market.close, 5) is actual_std
        assert rolling_quantile(market.close, 5, 0.25) is actual_quantile

    np.testing.assert_allclose(actual_sum, expected.rolling(5).sum(), equal_nan=True)
    np.testing.assert_allclose(actual_std, expected.rolling(5).std(ddof=0), atol=1e-6, equal_nan=True)
    np.testing.assert_allclose(
        actual_quantile,
        expected.rolling(5).quantile(0.25),
        atol=1e-6,
        equal_nan=True,
    )
    operations = cache.snapshot()["operations"]
    assert operations["rolling_sum"]["hits"] == 1
    assert operations["rolling_std"]["hits"] == 1
    assert operations["rolling_quantile"]["hits"] == 1


def test_builtin_matrix_strategy_formula_is_unchanged_with_cache():
    market = _market()
    strategy_path = (
        REPO_ROOT / "backend" / "app" / "strategy" / "builtin" / "macd_golden.py"
    )
    strategy_def = StrategyEngine._load_file(strategy_path)
    strategy = strategy_def.matrix_strategy
    assert strategy is not None
    params = {}
    uncached = strategy.compute_signals(market, params)
    cache = MatrixComputeCache(max_bytes=64 * 1024 * 1024)

    with cache.activate(market):
        first = strategy.compute_signals(market, params)
        second = strategy.compute_signals(market, params)

    np.testing.assert_array_equal(first.entry, uncached.entry)
    np.testing.assert_array_equal(second.entry, uncached.entry)
    assert cache.snapshot()["hits"] > 0


def test_pipeline_reuses_basic_asset_filter_and_raw_scoring_features():
    market = _market()
    cache = MatrixComputeCache(max_bytes=64 * 1024 * 1024)

    class AllEntries:
        def compute_signals(self, market, params):
            from app.backtest.matrix import make_signal_matrix

            return make_signal_matrix(
                market.shape,
                entry=np.ones(market.shape, dtype=np.uint8),
            )

    config = MatrixPipelineConfig(
        basic_filter={"enabled": True, "amount_min": 1.0},
        scoring={"momentum_5d": 1.0},
        order_by="score",
        descending=True,
        asset_mask=np.array([True, False]),
    )
    pipeline = MatrixStrategyPipeline()
    with cache.activate(market):
        first = pipeline.run(AllEntries(), market, {}, config)
        second = pipeline.run(AllEntries(), market, {}, config)

    np.testing.assert_array_equal(first.entry, second.entry)
    operations = cache.snapshot()["operations"]
    assert operations["basic_filter_mask"]["hits"] == 1
    assert operations["pipeline_filter_mask"]["hits"] == 1
    assert operations["matrix_feature"]["hits"] == 1


def test_pipeline_protects_strategy_working_set_when_scoring_would_overflow_cache():
    from app.backtest.matrix import make_signal_matrix

    market = _market()
    item_bytes = market.close.nbytes
    cache = MatrixComputeCache(max_bytes=item_bytes * 4)

    class RollingStrategy:
        def compute_signals(self, market, params):
            rolling_min(market.close, 3)
            rolling_max(market.close, 4)
            rolling_mean(market.close, 5)
            return make_signal_matrix(
                market.shape,
                entry=np.ones(market.shape, dtype=np.uint8),
            )

    config = MatrixPipelineConfig(
        basic_filter={"enabled": False},
        scoring={
            "momentum_5d": 0.3,
            "change_pct": 0.3,
            "vol_ratio_5d": 0.2,
            "momentum_20d": 0.2,
        },
        order_by="score",
        descending=True,
        protect_strategy_cache=True,
    )
    pipeline = MatrixStrategyPipeline()
    with cache.activate(market):
        pipeline.run(RollingStrategy(), market, {}, config)
        pipeline.run(RollingStrategy(), market, {}, config)

    operations = cache.snapshot()["operations"]
    assert operations["rolling_min"]["hits"] == 1
    assert operations["rolling_max"]["hits"] == 1
    assert operations["rolling_mean"]["hits"] == 1
    assert "matrix_feature" not in operations
    assert "basic_filter_mask" not in operations
