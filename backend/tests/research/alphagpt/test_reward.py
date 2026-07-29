from __future__ import annotations

import inspect

import pytest

from research.alphagpt.reward import RobustReward, TrainingFoldMetrics


def _fold(fold_id: str, *, icir: float, total_return: float, turnover: float):
    return TrainingFoldMetrics(
        fold_id=fold_id,
        start="2025-01-01",
        end="2025-03-31",
        mean_ic=0.02,
        icir=icir,
        total_return=total_return,
        turnover=turnover,
        top_decile_sharpe=1.0,
    )


def test_reward_components_are_complete_and_auditable() -> None:
    reward = RobustReward()
    result = reward.score(
        [
            _fold("T1", icir=1.0, total_return=0.10, turnover=0.20),
            _fold("T2", icir=0.5, total_return=-0.02, turnover=0.30),
            _fold("T3", icir=1.5, total_return=0.05, turnover=0.25),
        ],
        complexity=6,
        max_abs_correlation=0.4,
    )

    assert result.median_icir == 1.0
    assert result.positive_return_fold_ratio == pytest.approx(2 / 3)
    assert set(result.penalties) == {
        "turnover",
        "complexity",
        "fold_variance",
        "correlation",
    }
    assert result.training_fold_ids == ("T1", "T2", "T3")
    assert "train" in result.formula


def test_reward_api_has_no_test_or_holdout_input() -> None:
    parameters = inspect.signature(RobustReward.score).parameters
    assert "test_folds" not in parameters
    assert "holdout" not in parameters
    with pytest.raises(ValueError, match="training folds only"):
        TrainingFoldMetrics(
            fold_id="HOLDOUT",
            start="2026-04-01",
            end="2026-06-30",
            mean_ic=0.1,
            icir=9.0,
            total_return=9.0,
            turnover=0.0,
            top_decile_sharpe=9.0,
            dataset_role="test",
        )


def test_reward_rejects_non_training_metric_objects() -> None:
    with pytest.raises(TypeError, match="TrainingFoldMetrics"):
        RobustReward().score(  # type: ignore[arg-type]
            [{"fold_id": "test", "icir": 99.0}],
            complexity=1,
        )
