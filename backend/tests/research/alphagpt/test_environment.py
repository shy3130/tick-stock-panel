from __future__ import annotations

import numpy as np
import pytest

from research.alphagpt.environment import STOP_ACTION, AlphaEnv, AlphaEnvConfig
from research.common.factor_dsl import FEATURE_NAMES, StackVM


def _features() -> dict[str, np.ndarray]:
    base = np.linspace(1.0, 2.0, 64)
    return {name: base + index * 0.01 for index, name in enumerate(FEATURE_NAMES)}


def test_action_mask_prevents_underflow_and_illegal_stop() -> None:
    env = AlphaEnv(AlphaEnvConfig(max_formula_length=8, max_complexity=16, seed=7))
    valid = env.valid_actions()
    assert set(valid) == set(FEATURE_NAMES)
    assert STOP_ACTION not in valid
    with pytest.raises(ValueError, match="illegal action"):
        env.step("ADD")
    with pytest.raises(ValueError, match="illegal action"):
        env.step(STOP_ACTION)

    env.step("MOM20")
    assert STOP_ACTION in env.valid_actions()
    assert "ADD" not in env.valid_actions()
    assert "GATE" not in env.valid_actions()


def test_samples_10_000_stack_safe_and_stackvm_executable() -> None:
    env = AlphaEnv(AlphaEnvConfig(max_formula_length=10, max_complexity=20, seed=20260723))
    vm = StackVM()
    features = _features()
    for formula in env.sample_formulas(10_000):
        assert env.validate_formula(formula)
        assert vm.execute(formula, features) is not None


def test_same_seed_produces_identical_candidate_sequence() -> None:
    config = AlphaEnvConfig(max_formula_length=10, max_complexity=20, seed=101)
    left = AlphaEnv(config).sample_formulas(250)
    right = AlphaEnv(config).sample_formulas(250)
    assert left == right


def test_length_and_complexity_limits_are_hard_constraints() -> None:
    config = AlphaEnvConfig(max_formula_length=5, max_complexity=6, seed=9)
    env = AlphaEnv(config)
    for formula in env.sample_formulas(500):
        assert len(formula) <= config.max_formula_length
        assert env.formula_complexity(formula) <= config.max_complexity
