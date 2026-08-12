from __future__ import annotations

import numpy as np

from research.alphagpt.environment import AlphaEnv
from research.alphagpt.rank_model import (
    FormulaRanker,
    pairwise_accuracy,
    select_ranker_by_group_cv,
)
from research.alphagpt.reward_model import FormulaFeatureConfig


def _toy_data() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rows = []
    rewards = []
    groups = []
    for group in range(4):
        for value in range(8):
            rows.append([value, value * value / 10, group % 2])
            rewards.append(float(value) + group * 0.01)
            groups.append(group)
    return (
        np.asarray(rows, dtype=float),
        np.asarray(rewards, dtype=float),
        np.asarray(groups, dtype=int),
    )


def test_pairwise_and_listwise_rankers_learn_order() -> None:
    features, rewards, groups = _toy_data()
    for objective in ("pairwise", "listwise"):
        model = FormulaRanker(objective=objective, alpha=1.0).fit(
            features,
            rewards,
            groups,
        )
        assert pairwise_accuracy(rewards, model.predict(features)) > 0.9


def test_group_cv_is_deterministic_and_train_seed_only() -> None:
    features, rewards, groups = _toy_data()
    first = select_ranker_by_group_cv(
        features,
        rewards,
        groups,
        objectives=("pairwise", "listwise"),
        alphas=(0.1, 1.0),
    )
    second = select_ranker_by_group_cv(
        features,
        rewards,
        groups,
        objectives=("pairwise", "listwise"),
        alphas=(0.1, 1.0),
    )
    assert first == second
    assert first["selected_objective"] in {"pairwise", "listwise"}
    assert {
        fold["held_out_seed"]
        for candidate in first["candidates"]
        for fold in candidate["folds"]
    } == {0, 1, 2, 3}


def test_ranker_checkpoint_round_trips_exactly(tmp_path) -> None:
    features, rewards, groups = _toy_data()
    model = FormulaRanker(objective="pairwise", alpha=1.0).fit(
        features,
        rewards,
        groups,
    )
    environment = AlphaEnv()
    feature_config = FormulaFeatureConfig(
        action_space=environment.action_space,
        max_formula_length=environment.config.max_formula_length,
        max_complexity=environment.config.max_complexity,
    )
    path = tmp_path / "ranker.npz"
    before = model.predict(features)
    model.save(path, feature_config=feature_config)
    restored, restored_config = FormulaRanker.load(path)
    assert restored_config == feature_config
    assert np.array_equal(before, restored.predict(features))
