from __future__ import annotations

import json

import numpy as np
import pytest

from research.alphagpt.environment import AlphaEnv
from research.alphagpt.pool import formula_hash
from research.alphagpt.reward_model import (
    FormulaFeatureConfig,
    FormulaFeaturizer,
    FormulaRewardExample,
    RidgeRewardModel,
    load_formula_reward_examples,
    regression_metrics,
    select_alpha_by_training_cv,
    top_k_metrics,
)


def _feature_config() -> FormulaFeatureConfig:
    environment = AlphaEnv()
    return FormulaFeatureConfig(
        action_space=environment.action_space,
        max_formula_length=environment.config.max_formula_length,
        max_complexity=environment.config.max_complexity,
    )


def _example(
    index: int,
    tokens: tuple[str, ...],
    reward: float,
    *,
    split: str = "train",
) -> FormulaRewardExample:
    return FormulaRewardExample(
        formula_hash=f"{index:016x}" + "0" * 48,
        tokens=tokens,
        reward=reward,
        split=split,
    )


def test_formula_features_are_fixed_and_deterministic() -> None:
    featurizer = FormulaFeaturizer(_feature_config())
    example = _example(0, ("RET", "MOM5", "ADD"), 1.0)

    first = featurizer.transform([example])
    second = featurizer.transform([example])

    assert np.array_equal(first, second)
    assert first.shape == (1, len(featurizer.feature_names))
    assert np.isfinite(first).all()


def test_formula_features_reject_invalid_rpn() -> None:
    featurizer = FormulaFeaturizer(_feature_config())
    with pytest.raises(ValueError, match="valid RPN"):
        featurizer.transform_one(("RET", "MOM5"))


def test_ridge_model_learns_ranking_and_round_trips(tmp_path) -> None:
    examples = [
        _example(index, ("RET",) if index < 6 else ("RET", "MOM5", "ADD"), reward)
        for index, reward in enumerate([-3.2, -3.1, -3.0, -2.9, -2.8, -2.7, 2.7, 2.8, 2.9, 3.0, 3.1, 3.2])
    ]
    featurizer = FormulaFeaturizer(_feature_config())
    features = featurizer.transform(examples)
    targets = np.asarray([example.reward for example in examples])
    model = RidgeRewardModel(alpha=1.0).fit(features, targets)
    before = model.predict(features)

    assert regression_metrics(targets, before)["spearman"] > 0.85

    checkpoint = tmp_path / "reward_model.npz"
    model.save(checkpoint, feature_config=featurizer.config)
    restored, restored_config = RidgeRewardModel.load(checkpoint)

    assert restored_config == featurizer.config
    assert np.array_equal(before, restored.predict(features))


def test_training_cv_is_hash_deterministic_and_selects_candidate() -> None:
    token_sets = (
        ("RET",),
        ("MOM5",),
        ("RET", "MOM5", "ADD"),
        ("RET", "MOM5", "SUB"),
    )
    examples = [
        _example(index, token_sets[index % len(token_sets)], float(index % 4))
        for index in range(16)
    ]
    features = FormulaFeaturizer(_feature_config()).transform(examples)

    first = select_alpha_by_training_cv(
        examples,
        features,
        alphas=(0.1, 1.0, 10.0),
        n_folds=4,
    )
    second = select_alpha_by_training_cv(
        examples,
        features,
        alphas=(0.1, 1.0, 10.0),
        n_folds=4,
    )

    assert first == second
    assert first["selected_alpha"] in (0.1, 1.0, 10.0)
    assert len(first["candidates"]) == 3


def test_top_k_metrics_measure_realized_lift() -> None:
    result = top_k_metrics(
        actual=(-2.0, -1.0, 1.0, 3.0),
        predicted=(-1.5, -0.5, 0.5, 2.0),
        fraction=0.5,
    )
    assert result["count"] == 2
    assert result["selected_actual_mean"] == 2.0
    assert result["absolute_lift"] == pytest.approx(1.75)


def test_reward_loader_rejects_non_training_fold(tmp_path) -> None:
    digest = formula_hash(("RET",))
    manifest = {
        "episodes": [
            {
                "formula_hash": digest,
                "formula_tokens": ["RET"],
                "final_reward": 1.0,
                "split": "train",
                "training_fold_metrics": [{"dataset_role": "holdout"}],
            }
        ]
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="non-training fold"):
        load_formula_reward_examples(path)


def test_reward_loader_rejects_hash_mismatch_and_duplicate_formula(tmp_path) -> None:
    episode = {
        "formula_hash": formula_hash(("RET",)),
        "formula_tokens": ["RET"],
        "final_reward": 1.0,
        "split": "train",
        "training_fold_metrics": [{"dataset_role": "train"}],
    }
    mismatch = tmp_path / "mismatch.json"
    mismatch.write_text(
        json.dumps({"episodes": [{**episode, "formula_hash": "0" * 64}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not match"):
        load_formula_reward_examples(mismatch)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        json.dumps({"episodes": [episode, {**episode, "split": "validation"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate formula"):
        load_formula_reward_examples(duplicate)
