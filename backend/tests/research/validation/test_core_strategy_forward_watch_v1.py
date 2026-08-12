from __future__ import annotations

from research.validation.run_core_strategy_forward_watch_v1 import (
    MINIMUM_TRADING_DAYS,
    pair_comparison,
    readiness_gate,
)


def _result(total_return: float, max_drawdown: float = -0.1, n_trades: int = 20):
    return {
        "stats": {
            "total_return": total_return,
            "max_drawdown": max_drawdown,
            "n_trades": n_trades,
        }
    }


def test_short_observation_can_never_be_ready():
    gate = readiness_gate(MINIMUM_TRADING_DAYS - 1, {"x": {"return_delta": 9.0}})
    assert gate["status"] == "PENDING_DATA"
    assert gate["remaining_to_minimum"] == 1
    assert gate["auto_promote"] is False


def test_minimum_days_only_unlocks_review_not_promotion():
    gate = readiness_gate(MINIMUM_TRADING_DAYS, {})
    assert gate["status"] == "READY_FOR_FROZEN_REVIEW"
    assert gate["auto_promote"] is False


def test_pair_comparison_is_explicit_and_does_not_promote():
    comparison = pair_comparison(_result(0.12, -0.08), _result(0.03, -0.07))
    assert comparison["status"] == "OBSERVED_NOT_PROMOTED"
    assert comparison["return_delta"] == 0.09
    assert comparison["snapshot_conditions"] == {
        "candidate_positive": True,
        "beats_baseline": True,
        "drawdown_within_3pp": True,
    }


def test_pair_comparison_records_incomplete_results():
    comparison = pair_comparison({"error": "candidate failed"}, _result(0.03))
    assert comparison["status"] == "INCOMPLETE"
    assert comparison["candidate_error"] == "candidate failed"
