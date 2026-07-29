from __future__ import annotations

from research.optimization.run_bullish_breadth_walkforward_v1 import (
    BREADTH_CANDIDATES,
    DEFAULT_CANDIDATE_ID,
    next_training_mode_candidate,
    select_training_candidate,
    validate_candidates,
)


def _row(candidate_id: str, score):
    candidate = next(item for item in BREADTH_CANDIDATES if item["id"] == candidate_id)
    return {
        "candidate": dict(candidate),
        "status": "ok",
        "training_score": score,
        "stats": {"total_return": 0.1, "sharpe": 1.0},
    }


def test_breadth_v1_has_exactly_three_fixed_candidates():
    validate_candidates()
    assert len(BREADTH_CANDIDATES) == 3
    assert sum(item["id"] == DEFAULT_CANDIDATE_ID for item in BREADTH_CANDIDATES) == 1


def test_breadth_selection_is_deterministic():
    rows = [
        _row("breadth_conservative_cash", 0.2),
        _row("breadth_balanced_soft_30", 0.2),
    ]
    assert select_training_candidate(rows)["candidate"]["id"] == "breadth_balanced_soft_30"
    assert select_training_candidate(list(reversed(rows)))["candidate"]["id"] == "breadth_balanced_soft_30"


def test_breadth_selection_falls_back_to_default():
    rows = [_row(candidate["id"], None) for candidate in BREADTH_CANDIDATES]
    selected = select_training_candidate(rows)
    assert selected["candidate"]["id"] == DEFAULT_CANDIDATE_ID
    assert selected["selection_fallback"] == "default_no_eligible_candidate"


def test_breadth_mode_candidate_does_not_read_oos():
    folds = [
        {"selected_candidate": dict(BREADTH_CANDIDATES[1]), "oos_stats": {"total_return": -9}},
        {"selected_candidate": dict(BREADTH_CANDIDATES[1]), "oos_stats": {"total_return": -8}},
        {"selected_candidate": dict(BREADTH_CANDIDATES[2]), "oos_stats": {"total_return": 99}},
    ]
    before = next_training_mode_candidate(folds)
    for fold in folds:
        fold["oos_stats"]["total_return"] *= -1000
    assert next_training_mode_candidate(folds) == before
    assert before["candidate"]["id"] == BREADTH_CANDIDATES[1]["id"]
