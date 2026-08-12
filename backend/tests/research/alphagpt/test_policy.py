from __future__ import annotations

import numpy as np
import pytest

from research.alphagpt.environment import AlphaEnv, AlphaEnvConfig
from research.alphagpt.policy import (
    MaskedLogitPolicy,
    PolicyActionError,
    PolicyObservation,
    RandomTokenPolicy,
    ReplayTokenPolicy,
    masked_logits,
)
from research.alphagpt.rollouts import run_policy_episode


def test_masked_logit_policy_cannot_choose_highest_illegal_action() -> None:
    environment = AlphaEnv(AlphaEnvConfig(seed=7))
    observation = PolicyObservation.from_environment(environment)
    logits = np.zeros(len(observation.action_space))
    logits[observation.action_space.index("ADD")] = 100.0
    logits[observation.action_space.index("MOM20")] = 10.0
    policy = MaskedLogitPolicy(lambda _: logits, seed=7)

    assert policy.select_action(observation) == "MOM20"
    assert np.isneginf(
        masked_logits(logits, observation.action_mask)[
            observation.action_space.index("ADD")
        ]
    )


def test_masked_logits_rejects_bad_shape_and_non_finite_legal_value() -> None:
    environment = AlphaEnv()
    observation = PolicyObservation.from_environment(environment)
    with pytest.raises(ValueError, match="shape mismatch"):
        masked_logits([0.0], observation.action_mask)

    logits = np.zeros(len(observation.action_space))
    logits[0] = np.nan
    with pytest.raises(PolicyActionError, match="NaN"):
        masked_logits(logits, observation.action_mask)


def test_central_runner_rejects_policy_that_returns_masked_action() -> None:
    policy = ReplayTokenPolicy(["ADD"])
    with pytest.raises(PolicyActionError, match="masked action"):
        run_policy_episode(
            policy=policy,
            environment_config=AlphaEnvConfig(seed=9),
            episode_id="bad",
            seed=9,
        )


def test_random_policy_rollout_is_deterministic_and_valid() -> None:
    config = AlphaEnvConfig(max_formula_length=8, max_complexity=16, seed=101)
    left = run_policy_episode(
        policy=RandomTokenPolicy(seed=101),
        environment_config=config,
        episode_id="left",
        seed=101,
    )
    right = run_policy_episode(
        policy=RandomTokenPolicy(seed=101),
        environment_config=config,
        episode_id="right",
        seed=101,
    )

    assert left.formula_tokens == right.formula_tokens
    assert [step.action for step in left.steps] == [step.action for step in right.steps]
    for step in left.steps:
        assert step.observation.action_mask[step.action_id]
