from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from research.regime.market_structure import (
    STRUCTURAL_BEAR,
    STRUCTURAL_BULL,
    MarketStructureConfig,
    build_market_structure_features,
    classify_market_structure,
    market_structure_segments,
)


def _feature_rows(short_values, long_values, returns):
    start = date(2024, 1, 1)
    return pl.DataFrame(
        {
            "date": [start + timedelta(days=index) for index in range(len(short_values))],
            "asset_count": [100] * len(short_values),
            "breadth_short_valid": [100] * len(short_values),
            "breadth_long_valid": [100] * len(short_values),
            "return_valid": [100] * len(short_values),
            "breadth_short": short_values,
            "breadth_long": long_values,
            "advance_ratio": [0.5] * len(short_values),
            "ew_return_1d": [0.0] * len(short_values),
            "ew_market_level": [1.0] * len(short_values),
            "ew_return_20d": returns,
            "advance_ratio_10d": [0.5] * len(short_values),
            "structure_score": [0.5] * len(short_values),
        }
    )


def _config():
    return MarketStructureConfig(min_valid_assets=10, confirm_days=2)


def test_classification_uses_prior_day_and_two_day_confirmation():
    features = _feature_rows(
        [0.60, 0.60, 0.60, 0.40, 0.40, 0.40],
        [0.55, 0.55, 0.55, 0.35, 0.35, 0.35],
        [0.02, 0.02, 0.02, -0.05, -0.05, -0.05],
    )

    labels = classify_market_structure(features, _config())

    assert labels["regime"].to_list()[2] == STRUCTURAL_BULL
    # Day 3 has bear features, but those can only affect day 4; two confirmations
    # then change the tradable state on day 5.
    assert labels["regime"].to_list()[4] == STRUCTURAL_BULL
    assert labels["regime"].to_list()[5] == STRUCTURAL_BEAR


def test_same_day_feature_mutation_cannot_change_same_day_label():
    base = _feature_rows(
        [0.60, 0.60, 0.60],
        [0.55, 0.55, 0.55],
        [0.02, 0.02, 0.02],
    )
    changed = base.with_columns(
        pl.when(pl.arange(0, pl.len()) == 2)
        .then(pl.lit(0.0))
        .otherwise(pl.col("breadth_short"))
        .alias("breadth_short"),
        pl.when(pl.arange(0, pl.len()) == 2)
        .then(pl.lit(0.0))
        .otherwise(pl.col("breadth_long"))
        .alias("breadth_long"),
        pl.when(pl.arange(0, pl.len()) == 2)
        .then(pl.lit(-0.5))
        .otherwise(pl.col("ew_return_20d"))
        .alias("ew_return_20d"),
    )

    base_labels = classify_market_structure(base, _config())
    changed_labels = classify_market_structure(changed, _config())

    assert base_labels["regime"][2] == changed_labels["regime"][2]
    assert base_labels["decision_reason"][2] == changed_labels["decision_reason"][2]


def test_feature_builder_uses_returns_not_raw_price_level():
    rows = []
    start = date(2024, 1, 1)
    for index in range(65):
        trading_day = start + timedelta(days=index)
        rows.extend(
            [
                {"symbol": "A", "date": trading_day, "close": 10.0 * 1.01**index},
                {"symbol": "B", "date": trading_day, "close": 100.0 * 1.01**index},
            ]
        )

    features = build_market_structure_features(
        pl.DataFrame(rows),
        MarketStructureConfig(min_valid_assets=2),
    )

    assert abs(features["ew_return_1d"][1] - 0.01) < 1e-12
    assert features["breadth_long"][-1] == 1.0


def test_segments_are_contiguous_and_preserve_warmup():
    labels = pl.DataFrame(
        {
            "date": [
                date(2024, 1, 1),
                date(2024, 1, 2),
                date(2024, 1, 3),
                date(2024, 1, 4),
            ],
            "regime": ["warmup", STRUCTURAL_BULL, STRUCTURAL_BULL, STRUCTURAL_BEAR],
        }
    )

    assert market_structure_segments(labels) == [
        {
            "regime": "warmup",
            "start": date(2024, 1, 1),
            "end": date(2024, 1, 1),
            "trading_days": 1,
        },
        {
            "regime": STRUCTURAL_BULL,
            "start": date(2024, 1, 2),
            "end": date(2024, 1, 3),
            "trading_days": 2,
        },
        {
            "regime": STRUCTURAL_BEAR,
            "start": date(2024, 1, 4),
            "end": date(2024, 1, 4),
            "trading_days": 1,
        },
    ]
