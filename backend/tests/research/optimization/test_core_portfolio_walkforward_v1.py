from __future__ import annotations

import copy

import pytest

from research.optimization.run_core_portfolio_walkforward_v1 import (
    CANDIDATES,
    DEFAULT_CANDIDATE_ID,
    MIN_TRAIN_TRADES,
    V1_STRATEGY_IDS,
    next_frozen_candidate,
    select_training_candidate,
    training_score,
    validate_candidates,
)


def _row(candidate_id: str, score: float, total_return: float = 0.1, sharpe: float = 1.0):
    candidate = next(item for item in CANDIDATES if item["id"] == candidate_id)
    return {
        "candidate": dict(candidate),
        "status": "ok",
        "training_score": score,
        "stats": {"total_return": total_return, "sharpe": sharpe},
    }


def test_candidate_budget_is_fixed_and_has_explicit_default():
    validate_candidates()
    assert len(CANDIDATES) == 7
    assert len({candidate["id"] for candidate in CANDIDATES}) == 7
    assert any(candidate["id"] == DEFAULT_CANDIDATE_ID for candidate in CANDIDATES)
    assert len(V1_STRATEGY_IDS) == 5


def test_training_score_penalizes_drawdown_and_rejects_small_samples():
    assert training_score({
        "total_return": 0.20,
        "max_drawdown": -0.10,
        "n_trades": MIN_TRAIN_TRADES,
    }) == pytest.approx(0.15)
    assert training_score({
        "total_return": 0.20,
        "max_drawdown": -0.10,
        "n_trades": MIN_TRAIN_TRADES - 1,
    }) == float("-inf")


def test_training_selection_is_deterministic_with_lexical_final_tie_break():
    rows = [_row("equal_5", 0.2), _row("equal_20", 0.2)]
    assert select_training_candidate(rows)["candidate"]["id"] == "equal_20"
    assert select_training_candidate(list(reversed(rows)))["candidate"]["id"] == "equal_20"


def test_training_selection_falls_back_to_explicit_default():
    rows = [_row(candidate["id"], None) for candidate in CANDIDATES]
    selected = select_training_candidate(rows)
    assert selected["candidate"]["id"] == DEFAULT_CANDIDATE_ID
    assert selected["selection_fallback"] == "default_no_eligible_candidate"


def test_next_candidate_never_reads_oos_metrics():
    folds = [
        {"selected_candidate": dict(CANDIDATES[0]), "oos_stats": {"total_return": -0.9}},
        {"selected_candidate": dict(CANDIDATES[0]), "oos_stats": {"total_return": -0.8}},
        {"selected_candidate": dict(CANDIDATES[1]), "oos_stats": {"total_return": 9.0}},
    ]
    before = next_frozen_candidate(folds)
    changed = copy.deepcopy(folds)
    for fold in changed:
        fold["oos_stats"]["total_return"] *= -1000
    assert next_frozen_candidate(changed) == before
    assert before["candidate"]["id"] == CANDIDATES[0]["id"]
