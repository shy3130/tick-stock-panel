from __future__ import annotations

import numpy as np

from research.alphagpt.environment import AlphaEnv, AlphaEnvConfig
from research.alphagpt.pool import formula_hash
from research.alphagpt.reranker import (
    generate_candidate_slate,
    random_baseline_selection,
    score_candidate_slate,
)
from research.alphagpt.reward_model import (
    FormulaFeatureConfig,
    FormulaFeaturizer,
    FormulaRewardExample,
    RidgeRewardModel,
)


def _model() -> tuple[RidgeRewardModel, FormulaFeaturizer]:
    environment = AlphaEnv()
    config = FormulaFeatureConfig(
        action_space=environment.action_space,
        max_formula_length=environment.config.max_formula_length,
        max_complexity=environment.config.max_complexity,
    )
    featurizer = FormulaFeaturizer(config)
    examples = [
        FormulaRewardExample("0" * 64, ("RET",), -2.0, "train"),
        FormulaRewardExample("1" * 64, ("MOM5",), -1.0, "train"),
        FormulaRewardExample("2" * 64, ("RET", "MOM5", "ADD"), 2.0, "train"),
        FormulaRewardExample("3" * 64, ("RET", "MOM5", "MUL"), 3.0, "train"),
    ]
    features = featurizer.transform(examples)
    model = RidgeRewardModel(alpha=1.0).fit(
        features,
        np.asarray([example.reward for example in examples]),
    )
    return model, featurizer


def test_candidate_slate_is_deterministic_unique_and_excludes_sources() -> None:
    config = AlphaEnvConfig(seed=7)
    excluded = {formula_hash(("RET",))}
    first = generate_candidate_slate(
        environment_config=config,
        seed=123,
        count=30,
        excluded_hashes=excluded,
    )
    second = generate_candidate_slate(
        environment_config=config,
        seed=123,
        count=30,
        excluded_hashes=excluded,
    )

    assert first == second
    assert len({formula_hash(tokens) for tokens in first}) == 30
    assert not ({formula_hash(tokens) for tokens in first} & excluded)
    assert all(AlphaEnv(config).validate_formula(tokens) for tokens in first)


def test_scoring_and_random_baseline_are_deterministic() -> None:
    model, featurizer = _model()
    slate = [("RET",), ("MOM5",), ("RET", "MOM5", "ADD")]

    scored = score_candidate_slate(slate, model=model, featurizer=featurizer)
    assert [item.slate_rank for item in scored] == [1, 2, 3]
    assert scored[0].predicted_reward >= scored[-1].predicted_reward

    first = random_baseline_selection(scored, count=2, seed=99)
    second = random_baseline_selection(scored, count=2, seed=99)
    assert first == second
