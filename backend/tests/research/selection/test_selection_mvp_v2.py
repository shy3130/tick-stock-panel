from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest

from research.selection.mvp_v2 import (
    BASE_VARIANT,
    FACTOR_VARIANT,
    InstrumentWindow,
    TradingCosts,
    build_forward_open_labels,
    combine_factor_overlay,
    cross_sectional_percentiles,
    dynamic_universe_mask,
    evaluate_score_grid,
    generate_session_folds,
    preferred_live_variant,
    select_variant_from_training,
)


def _market(open_prices: np.ndarray) -> SimpleNamespace:
    shape = open_prices.shape
    return SimpleNamespace(
        open=open_prices,
        tradable=np.ones(shape, dtype=bool),
        limit_up_locked=np.zeros(shape, dtype=bool),
        limit_down_locked=np.zeros(shape, dtype=bool),
    )


def _metrics(excess: float, drawdown: float) -> dict:
    return {
        "5": {
            "10": {
                "mean_excess_return": excess,
                "phase_portfolios": {"worst_max_drawdown": drawdown},
            }
        }
    }


def test_dynamic_universe_applies_listing_dates_and_current_non_st_proxy():
    labels = ("2025-01-02", "2025-01-03", "2025-01-06")
    symbols = ("000001.SZ", "000002.SZ", "000003.SZ")
    present = np.ones((3, 3), dtype=bool)
    windows = {
        symbols[0]: InstrumentWindow(symbols[0], "正常", None, None, True),
        symbols[1]: InstrumentWindow(symbols[1], "次新", date(2025, 1, 3), None, True),
        symbols[2]: InstrumentWindow(symbols[2], "ST样本", None, None, False),
    }

    mask, summary = dynamic_universe_mask(labels, symbols, windows, present)

    assert mask.tolist() == [[True, False, False], [True, True, False], [True, True, False]]
    assert summary["excluded_current_st_or_delisting_name"] == 1


def test_cross_sectional_percentiles_and_overlay_are_deterministic():
    values = np.array([[1.0, 1.0, 3.0]], dtype=np.float32)
    eligible = np.ones_like(values, dtype=bool)
    symbols = ("000002.SZ", "000001.SZ", "000003.SZ")

    first = cross_sectional_percentiles(values, eligible, symbols)
    second = cross_sectional_percentiles(values, eligible, symbols)
    combined = combine_factor_overlay(first, first, factor_weight=0.2)

    np.testing.assert_array_equal(first, second)
    np.testing.assert_allclose(first, combined)
    assert first[0, 1] < first[0, 0] < first[0, 2]


def test_forward_labels_use_next_open_and_record_tradeability_failures():
    market = _market(
        np.array(
            [
                [10.0, 10.0],
                [11.0, 10.0],
                [12.1, 11.0],
                [13.31, 12.0],
            ],
            dtype=np.float32,
        )
    )
    market.limit_up_locked[1, 1] = True

    label = build_forward_open_labels(market, horizons=(1,))[1]

    assert label["gross_return"][0, 0] == pytest.approx(0.1)
    assert not label["valid"][0, 1]
    assert np.isnan(label["gross_return"][0, 1])


def test_walk_forward_folds_are_expanding_neither_random_nor_overlapping_train_test():
    first = generate_session_folds(range(14), train_sessions=6, test_sessions=3, step_sessions=2)
    second = generate_session_folds(range(14), train_sessions=6, test_sessions=3, step_sessions=2)

    assert first == second
    assert len(first) == 3
    assert set(first[0].train_ids).isdisjoint(first[0].test_ids)
    assert first[0].test_ids == (6, 7, 8)


def test_factor_choice_accepts_training_metrics_only_and_base_wins_ties():
    decision = select_variant_from_training(
        {
            BASE_VARIANT: _metrics(0.02, -0.04),
            FACTOR_VARIANT: _metrics(0.03, -0.03),
        },
        improvement_margin=0.001,
    )

    assert decision["selected_variant"] == FACTOR_VARIANT
    assert "test" not in select_variant_from_training.__code__.co_varnames
    assert (
        preferred_live_variant([BASE_VARIANT, FACTOR_VARIANT])["selected_variant"] == BASE_VARIANT
    )


def test_evaluation_ranks_without_looking_at_future_label_validity():
    scores = np.array([[3.0, 2.0, 1.0]], dtype=np.float32)
    eligible = np.ones_like(scores, dtype=bool)
    labels = {
        1: {
            "gross_return": np.array([[0.1, 0.2, 0.3]], dtype=np.float32),
            "valid": np.array([[False, True, True]], dtype=bool),
        }
    }

    _, records = evaluate_score_grid(
        scores=scores,
        eligible=eligible,
        symbols=("A", "B", "C"),
        timestamp_labels=("2025-01-02",),
        forward_labels=labels,
        time_ids=(0,),
        costs=TradingCosts(),
        horizons=(1,),
        top_ks=(1,),
    )

    row = records[(1, 1)][0]
    assert row["selected_ids"] == (0,)
    assert row["valid_selected"] == 0
    assert row["net_return"] is None
