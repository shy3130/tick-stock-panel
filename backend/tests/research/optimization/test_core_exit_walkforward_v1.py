from __future__ import annotations

from research.optimization.run_core_exit_walkforward_v1 import (
    DEFAULT_CANDIDATE_ID,
    EXIT_CANDIDATES,
    next_training_mode_candidate,
    select_training_candidate,
    validate_candidates,
)


def _row(candidate_id: str, score):
    candidate = next(item for item in EXIT_CANDIDATES if item["id"] == candidate_id)
    return {
        "candidate": dict(candidate),
        "status": "ok",
        "training_score": score,
        "stats": {"total_return": 0.1, "sharpe": 1.0},
    }


def test_exit_candidate_budget_is_fixed_and_has_default():
    validate_candidates()
    assert len(EXIT_CANDIDATES) == 7
    assert len({candidate["id"] for candidate in EXIT_CANDIDATES}) == 7
    assert any(candidate["id"] == DEFAULT_CANDIDATE_ID for candidate in EXIT_CANDIDATES)


def test_exit_selection_is_order_independent_and_lexically_tied():
    rows = [_row("stop_3", 0.2), _row("hold_8", 0.2)]
    assert select_training_candidate(rows)["candidate"]["id"] == "hold_8"
    assert select_training_candidate(list(reversed(rows)))["candidate"]["id"] == "hold_8"


def test_exit_selection_explicitly_falls_back_to_default():
    rows = [_row(candidate["id"], None) for candidate in EXIT_CANDIDATES]
    selected = select_training_candidate(rows)
    assert selected["candidate"]["id"] == DEFAULT_CANDIDATE_ID
    assert selected["selection_fallback"] == "default_no_eligible_candidate"


def test_next_mode_candidate_uses_train_selections_only():
    folds = [
        {"selected_candidate": dict(EXIT_CANDIDATES[1]), "oos_stats": {"total_return": -9}},
        {"selected_candidate": dict(EXIT_CANDIDATES[1]), "oos_stats": {"total_return": -8}},
        {"selected_candidate": dict(EXIT_CANDIDATES[2]), "oos_stats": {"total_return": 99}},
    ]
    selected = next_training_mode_candidate(folds)
    assert selected["candidate"]["id"] == EXIT_CANDIDATES[1]["id"]
    for fold in folds:
        fold["oos_stats"]["total_return"] *= -1000
    assert next_training_mode_candidate(folds) == selected
