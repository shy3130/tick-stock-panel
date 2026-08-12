from __future__ import annotations

from research.optimization.run_structural_bull_challenge_v1 import (
    candidates,
    select_calibration_winner,
    strict_bull_generalization_pass,
    target_met,
)


def _row(candidate_id: str, win_rate: float, total_return: float, drawdown: float = -0.2):
    return {
        "candidate": {"id": candidate_id},
        "stats": {
            "win_rate": win_rate,
            "total_return": total_return,
            "max_drawdown": drawdown,
            "n_trades": 100,
        },
    }


def test_candidate_budget_is_deterministic_and_unique() -> None:
    rows = candidates()
    assert len(rows) == 30
    assert len({row["id"] for row in rows}) == len(rows)
    assert rows == candidates()


def test_target_requires_both_thresholds_and_minimum_trades() -> None:
    assert target_met(_row("ok", 0.60, 0.80)["stats"])
    assert not target_met(_row("win_only", 0.61, 0.79)["stats"])
    sparse = _row("sparse", 1.0, 2.0)["stats"]
    sparse["n_trades"] = 2
    assert not target_met(sparse)


def test_selection_prefers_balanced_target_candidate() -> None:
    selected = select_calibration_winner([
        _row("return_heavy", 0.601, 1.20),
        _row("balanced", 0.65, 0.90),
        _row("miss", 0.59, 1.50),
    ])
    assert selected["candidate"]["id"] == "balanced"


def test_generalization_uses_only_predeclared_bull_windows() -> None:
    rows = [
        {"is_predeclared_bull_window": True, **_row("a", 0.61, 0.81)},
        {"is_predeclared_bull_window": True, **_row("b", 0.60, 0.80)},
        {"is_predeclared_bull_window": False, **_row("post", 0.10, -0.50)},
    ]
    assert strict_bull_generalization_pass(rows)
    rows[1]["stats"]["win_rate"] = 0.59
    assert not strict_bull_generalization_pass(rows)
