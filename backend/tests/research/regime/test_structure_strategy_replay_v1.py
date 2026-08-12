from __future__ import annotations

from research.regime.run_structure_strategy_replay_v1 import (
    _comparison,
    configurations,
)


def test_structure_replay_has_fixed_seven_configuration_budget():
    configs = configurations("protocol")

    assert len(configs) == 7
    assert [config["key"] for config in configs] == [
        "bullish_always",
        "bullish_bull_bear_cash",
        "bullish_bull_bear_pullback",
        "trend_always",
        "trend_bull_bear_cash",
        "trend_bull_bear_pullback",
        "pullback_always",
    ]
    switches = [config for config in configs if config["composition"]]
    assert len(switches) == 2
    assert all(
        config["composition"]["regime"]["protocol_hash"] == "protocol"
        for config in switches
    )


def test_historical_gate_requires_three_positive_and_three_beating_folds():
    folds = []
    candidate_returns = [0.10, 0.08, -0.02, 0.05]
    baseline_returns = [0.05, 0.04, -0.01, 0.01]
    for index, (candidate, baseline) in enumerate(
        zip(candidate_returns, baseline_returns, strict=True)
    ):
        folds.append(
            {
                "fold": f"F{index + 1}",
                "runs": [
                    {"key": "candidate", "total_return": candidate},
                    {"key": "baseline", "total_return": baseline},
                ],
            }
        )
    aggregate = {
        "candidate": {"positive_folds": 3, "compounded_return": 0.20},
        "baseline": {"positive_folds": 3, "compounded_return": 0.10},
    }

    result = _comparison("candidate", "baseline", folds, aggregate)

    assert result["beats_baseline_folds"] == 3
    assert result["historical_gate_pass"] is True
    assert result["auto_apply"] is False
