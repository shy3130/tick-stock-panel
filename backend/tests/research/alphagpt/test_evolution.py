from __future__ import annotations

import hashlib
import random

import numpy as np

from research.alphagpt.environment import AlphaEnv, AlphaEnvConfig
from research.alphagpt.evolution import (
    EvaluationOutcome,
    FormulaSearch,
    SearchConfig,
    crossover_formulas,
    mutate_formula,
    run_search_comparison,
)
from research.alphagpt.reward import RobustReward, RobustRewardConfig, TrainingFoldMetrics


def _evaluator(tokens) -> EvaluationOutcome:
    digest = hashlib.sha256(" ".join(tokens).encode()).digest()
    values = np.frombuffer(digest, dtype=np.uint8).astype(float)
    centered = (values - values.mean()) / (values.std() + 1e-9)
    base = (digest[0] / 255.0) * 2.0 - 1.0
    folds = tuple(
        TrainingFoldMetrics(
            fold_id=f"T{index + 1}",
            start=f"2025-0{index + 1}-01",
            end=f"2025-0{index + 2}-01",
            mean_ic=base / 100.0,
            icir=base + index * 0.05,
            total_return=base / 20.0 + index * 0.005,
            turnover=0.1 + digest[index + 1] / 1000.0,
            top_decile_sharpe=base,
        )
        for index in range(3)
    )
    return EvaluationOutcome(folds, centered)


def _configs():
    env = AlphaEnvConfig(
        max_formula_length=8,
        max_complexity=16,
        stop_probability=0.15,
        seed=123,
    )
    search = SearchConfig(
        candidate_budget=18,
        population_size=6,
        elite_size=3,
        correlation_threshold=1.0,
        final_candidate_count=3,
        seed=123,
    )
    reward = RobustReward(RobustRewardConfig(max_complexity=16))
    return env, search, reward


def test_mutation_and_crossover_keep_formulas_executable() -> None:
    env = AlphaEnv(AlphaEnvConfig(max_formula_length=10, max_complexity=20, seed=7))
    rng = random.Random(9)
    parents = env.sample_formulas(20)
    for index in range(100):
        left = parents[index % len(parents)]
        right = parents[(index + 1) % len(parents)]
        assert env.validate_formula(mutate_formula(left, rng=rng, environment=env))
        assert env.validate_formula(
            crossover_formulas(left, right, rng=rng, environment=env)
        )


def test_random_and_evolution_use_identical_evaluation_budget() -> None:
    env, search, reward = _configs()
    results = run_search_comparison(
        evaluator=_evaluator,
        environment_config=env,
        search_config=search,
        reward=reward,
    )

    assert results["random"].evaluations_used == search.candidate_budget
    assert results["evolution"].evaluations_used == search.candidate_budget
    assert results["random"].evaluation_budget == results["evolution"].evaluation_budget
    assert any(
        candidate.generation_method in {"mutation", "crossover"}
        for candidate in results["evolution"].pool.candidates.values()
    )


def test_same_seed_produces_identical_search_results() -> None:
    env, search, reward = _configs()
    left = run_search_comparison(
        evaluator=_evaluator,
        environment_config=env,
        search_config=search,
        reward=reward,
    )
    right = run_search_comparison(
        evaluator=_evaluator,
        environment_config=env,
        search_config=search,
        reward=reward,
    )

    assert {key: value.to_dict() for key, value in left.items()} == {
        key: value.to_dict() for key, value in right.items()
    }


def test_checkpoint_resume_matches_uninterrupted_run(tmp_path) -> None:
    env, search_config, reward = _configs()
    checkpoint = tmp_path / "evolution.checkpoint.json"
    interrupted = FormulaSearch(
        method="evolution",
        evaluator=_evaluator,
        environment_config=env,
        search_config=search_config,
        reward=reward,
        checkpoint_path=checkpoint,
    )
    for _ in range(3):
        formula = interrupted._sample_unique()
        assert formula is not None
        interrupted._evaluate(formula, generation_method="evolution_initial")

    resumed = FormulaSearch(
        method="evolution",
        evaluator=_evaluator,
        environment_config=env,
        search_config=search_config,
        reward=reward,
        checkpoint_path=checkpoint,
    ).run(resume=True)
    clean = FormulaSearch(
        method="evolution",
        evaluator=_evaluator,
        environment_config=env,
        search_config=search_config,
        reward=reward,
    ).run()

    assert resumed.to_dict() == clean.to_dict()
