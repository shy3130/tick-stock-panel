from __future__ import annotations

import numpy as np
import pytest

from app.backtest.matrix import make_signal_matrix, validate_signal_matrix
from app.strategy.composition import StrategyComposition, compose_signal_matrices


def _signals(entry, exit_, score):
    shape = np.asarray(entry).shape
    return make_signal_matrix(
        shape,
        entry=np.asarray(entry, dtype=np.uint8),
        exit=np.asarray(exit_, dtype=np.uint8),
        score=np.asarray(score, dtype=np.float32),
    )


def _composition(entry_mode: str = "and") -> StrategyComposition:
    return StrategyComposition.from_dict(
        {
            "entry_mode": entry_mode,
            "components": [
                {"strategy_id": "base", "weight": 3},
                {"strategy_id": "factor", "weight": 1},
            ],
        },
        primary_strategy_id="base",
    )


def test_composition_and_entries_any_exit_and_weighted_percentile_score():
    first = _signals(
        [[1, 1, 0], [1, 1, 1]],
        [[0, 0, 0], [0, 1, 0]],
        [[10, 20, 999], [1, 1, 3]],
    )
    second = _signals(
        [[1, 0, 1], [1, 1, 1]],
        [[0, 1, 0], [0, 0, 0]],
        [[30, 999, 10], [3, 2, 1]],
    )

    result = compose_signal_matrices([first, second], _composition("and"))

    np.testing.assert_array_equal(result.entry, [[1, 0, 0], [1, 1, 1]])
    np.testing.assert_array_equal(result.exit, [[0, 1, 0], [0, 1, 0]])
    # Row 0: first rank=1/2, second rank=1, weighted 3:1.
    assert result.score[0, 0] == pytest.approx(0.625)
    # Exact ties in the first component receive the same average rank.
    assert result.score[1, 0] == pytest.approx((0.5 * 3 + 1.0) / 4)
    assert result.score[1, 1] == pytest.approx((0.5 * 3 + 2 / 3) / 4)
    validate_signal_matrix(result, (2, 3))


def test_composition_or_keeps_union_but_zeros_non_entries():
    first = _signals([[1, 0, 0]], [[0, 0, 0]], [[2, 100, 100]])
    second = _signals([[0, 1, 0]], [[0, 0, 0]], [[100, 3, 100]])

    result = compose_signal_matrices([first, second], _composition("or"))

    np.testing.assert_array_equal(result.entry, [[1, 1, 0]])
    np.testing.assert_allclose(result.score, [[0.75, 0.25, 0.0]])


def test_regime_switch_uses_active_leg_and_exits_on_state_flip():
    bull = _signals(
        [[1, 0], [0, 1], [1, 1]],
        [[0, 0], [1, 0], [0, 0]],
        [[2, 0], [0, 3], [4, 5]],
    )
    bear = _signals(
        [[0, 1], [1, 0], [0, 1]],
        [[0, 0], [0, 1], [1, 0]],
        [[0, 6], [7, 0], [0, 8]],
    )
    composition = StrategyComposition.from_dict(
        {
            "entry_mode": "regime_switch",
            "regime": {"type": "market_structure_v1"},
            "components": [
                {"strategy_id": "base"},
                {"strategy_id": "factor"},
            ],
        },
        primary_strategy_id="base",
    )

    result = compose_signal_matrices(
        [bull, bear],
        composition,
        regime_allow=np.array([True, True, False]),
    )

    np.testing.assert_array_equal(result.entry, [[1, 0], [0, 1], [0, 1]])
    # Bull leg exit on row 1, then the bull->bear flip exits every existing leg.
    np.testing.assert_array_equal(result.exit, [[0, 0], [1, 0], [1, 1]])
    np.testing.assert_allclose(result.score, [[2, 0], [0, 3], [0, 8]])
    assert result.entry_signal_ids == ("regime:bull:base", "regime:bear:factor")


def test_regime_switch_requires_exactly_two_components_and_causal_regime():
    with pytest.raises(ValueError, match="exactly two"):
        StrategyComposition.from_dict(
            {
                "entry_mode": "regime_switch",
                "regime": {"type": "market_structure_v1"},
                "components": [
                    {"strategy_id": "base"},
                    {"strategy_id": "factor"},
                    {"strategy_id": "third"},
                ],
            },
            primary_strategy_id="base",
        )
    with pytest.raises(ValueError, match="market_structure_v1"):
        StrategyComposition.from_dict(
            {
                "entry_mode": "regime_switch",
                "components": [
                    {"strategy_id": "base"},
                    {"strategy_id": "factor"},
                ],
            },
            primary_strategy_id="base",
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"components": [{"strategy_id": "base"}]}, "2 to 8"),
        (
            {"components": [{"strategy_id": "other"}, {"strategy_id": "factor"}]},
            "first component",
        ),
        (
            {"components": [{"strategy_id": "base"}, {"strategy_id": "base"}]},
            "duplicate",
        ),
        (
            {
                "components": [
                    {"strategy_id": "base", "weight": 0},
                    {"strategy_id": "factor"},
                ]
            },
            "positive",
        ),
    ],
)
def test_composition_rejects_ambiguous_or_unsafe_configs(payload, message):
    with pytest.raises(ValueError, match=message):
        StrategyComposition.from_dict(payload, primary_strategy_id="base")


def test_composition_serialization_round_trips():
    composition = _composition("or")
    restored = StrategyComposition.from_dict(
        composition.to_dict(),
        primary_strategy_id="base",
    )
    assert restored == composition
