import math
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest.matrix import MatrixComputeCache, build_market_data_matrix, matrix_feature
from app.backtest.strategy import StrategyBacktestService
from app.strategy.engine import StrategyEngine
from app.strategy.scoring import (
    effective_scoring,
    materialize_scoring_columns,
    scoring_dependencies,
)


def _candidates() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["A", "B"],
        "date": [date(2024, 1, 2)] * 2,
        "close": [11.0, 12.0],
        "ma20": [10.0, 10.0],
        "vol_ratio_5d": [2.0, 1.0],
    })


@pytest.mark.parametrize(("factor", "expected", "fallback"), [
    ("vwap_bias", 0.0, -0.5),
    ("log_float_mv", 2.302585093, 1.609437912),
])
def test_quote_factors_match_across_frame_matrix_and_cache(factor, expected, fallback):
    frame = _candidates().with_columns(
        pl.lit(5.0).alias("close"), pl.lit(5.0).alias("open"),
        pl.lit(5.0).alias("high"), pl.lit(5.0).alias("low"),
        pl.Series("raw_close", [10.0, None]),
        pl.lit(1.0).alias("volume"), pl.lit(1000.0).alias("amount"),
        pl.lit(1.0).alias("turnover_rate"),
    )
    fields = {"raw_close", "amount", "turnover_rate"}
    market = build_market_data_matrix(frame, field_columns=fields)
    legacy = build_market_data_matrix(frame.drop("raw_close"), field_columns=fields - {"raw_close"})
    revised = replace(market, fields={**market.fields, "raw_close": market.field("raw_close") * 2})
    cache = MatrixComputeCache(max_bytes=1024 * 1024)

    results = materialize_scoring_columns(frame, {factor})[factor].to_list()
    assert results[0] == pytest.approx(expected)
    assert results[1] is None
    with cache.activate(market):
        values = matrix_feature(market, factor)
        assert values[0, 0] == pytest.approx(expected)
        assert math.isnan(values[0, 1])
        assert matrix_feature(market, factor) is values
        assert matrix_feature(revised, factor)[0, 0] != pytest.approx(expected)
    assert matrix_feature(legacy, factor)[0, 0] == pytest.approx(fallback)
    assert "raw_close" in scoring_dependencies({factor: 1.0})
    assert "raw_close" not in scoring_dependencies({"rsi_14": 1.0})


def test_virtual_scoring_is_shared_and_does_not_add_virtual_column():
    weights = {"ma20_bias": 0.6, "vol_ratio_5d": 0.4}
    realtime = StrategyEngine._apply_scoring(_candidates(), weights)
    strategy = SimpleNamespace(meta={"scoring": weights, "order_by": "score"})
    backtest = StrategyBacktestService._apply_score(_candidates(), strategy, None)

    assert realtime["score"].to_list() == pytest.approx([40.0, 60.0])
    assert backtest["score"].to_list() == pytest.approx([40.0, 60.0])
    assert "ma20_bias" not in realtime.columns
    assert "ma20_bias" not in backtest.columns


def test_scoring_reweights_only_available_fields():
    scored = StrategyEngine._apply_scoring(
        _candidates().drop("ma20"),
        {"ma20_bias": 0.6, "vol_ratio_5d": 0.4},
    )

    assert scored["score"].to_list() == pytest.approx([100.0, 0.0])


def test_scoring_can_prefer_lower_factor_values():
    scored = StrategyEngine._apply_scoring(
        _candidates(),
        {"ma20_bias": 0.6, "vol_ratio_5d": 0.4},
        {"ma20_bias": "low"},
    )

    assert scored["score"].to_list() == pytest.approx([100.0, 0.0])


def test_backtest_scoring_uses_saved_direction_and_replacement():
    strategy = SimpleNamespace(meta={
        "scoring": {"ma20_bias": 1.0},
        "order_by": "score",
        "descending": True,
    })

    scored = StrategyBacktestService._apply_score(
        _candidates(),
        strategy,
        {
            "scoring": {"vol_ratio_5d": 1.0},
            "scoring_directions": {"vol_ratio_5d": "low"},
            "scoring_replace": True,
        },
    )

    assert scored["score"].to_list() == pytest.approx([0.0, 100.0])


def test_realtime_scoring_materializes_rolling_factor_from_history():
    start = date(2024, 1, 1)
    history = pl.DataFrame({
        "symbol": [symbol for offset in range(11) for symbol in ("A", "B")],
        "date": [start + timedelta(days=offset) for offset in range(11) for _ in range(2)],
        "volume": [
            20.0 if symbol == "A" and offset == 10 else 10.0
            for offset in range(11)
            for symbol in ("A", "B")
        ],
    })
    current = history.filter(pl.col("date") == start + timedelta(days=10))

    scored_current, scored_history = StrategyEngine._materialize_scoring_frames(
        current,
        history,
        {"vol_ratio_10d": 1.0},
    )

    assert scored_current is not None
    assert scored_history is not None
    assert scored_current.sort("symbol")["vol_ratio_10d"].to_list() == pytest.approx([2.0, 1.0])
    assert scored_history["vol_ratio_10d"].drop_nulls().len() == 2


def test_effective_scoring_keeps_legacy_merge_and_supports_full_replace():
    defaults = {"momentum_20d": 0.6, "vol_ratio_5d": 0.4}

    assert effective_scoring(defaults, {"scoring": {"vol_ratio_5d": 0.8}}) == {
        "momentum_20d": 0.6,
        "vol_ratio_5d": 0.8,
    }
    assert effective_scoring(defaults, {
        "scoring": {"rsi_14": 1.0},
        "scoring_replace": True,
    }) == {"rsi_14": 1.0}
    assert effective_scoring(defaults, {
        "scoring": {},
        "scoring_replace": True,
    }) == {}
