from __future__ import annotations

from research.alphagpt.environment import STOP_ACTION, AlphaEnvConfig
from research.alphagpt.rollouts import (
    collect_p10_evolution_rollouts,
    replay_teacher_formula,
)
from research.alphagpt.pool import formula_hash


def _candidate(
    candidate_id: str,
    tokens: list[str],
    *,
    reward: float,
    status: str = "accepted",
):
    return {
        "candidate_id": candidate_id,
        "formula_hash": formula_hash(tokens),
        "formula": " ".join(tokens),
        "tokens": tokens,
        "parent_formulas": [],
        "generation_method": "mutation",
        "complexity": len(tokens),
        "fold_metrics": [
            {
                "fold_id": "T1",
                "start": "2025-01-01",
                "end": "2025-03-31",
                "dataset_role": "train",
                "icir": reward,
            }
        ],
        "reward": {
            "total": reward,
            "formula": "train only",
            "positive_components": {"median_icir": reward},
            "penalties": {},
        },
        "status": status,
        "rejection_reason": None,
        "max_abs_correlation": 0.0,
        "correlated_with": None,
    }


def p10_payload() -> dict:
    accepted = [
        _candidate("evolution_1", ["MOM20"], reward=1.0),
        _candidate("evolution_2", ["MOM5", "RET", "ADD"], reward=2.0),
    ]
    rejected = _candidate(
        "evolution_3",
        ["MA20_DEV"],
        reward=99.0,
        status="rejected",
    )
    return {
        "phase": "P10 AlphaGPT closed loop v1",
        "config": {
            "seed": 123,
            "environment": {
                "max_formula_length": 10,
                "max_complexity": 20,
                "min_formula_length": 1,
                "stop_probability": 0.18,
                "seed": 123,
            },
            "search": {"data_fingerprint": "training-data-only"},
        },
        "searches": {
            "evolution": {
                "pool": {"candidates": [*accepted, rejected]},
            }
        },
        "final_candidates": {
            "evolution": [{"holdout_metrics": {"icir": 999999.0}}],
        },
    }


def test_teacher_replay_records_every_legal_action_and_stop() -> None:
    tokens = ["MOM5", "RET", "ADD"]
    episode = replay_teacher_formula(
        tokens=tokens,
        environment_config=AlphaEnvConfig(seed=7),
        episode_id="teacher",
        seed=7,
    )

    assert episode.formula_tokens == tuple(tokens)
    assert episode.formula_hash == formula_hash(tokens)
    assert [step.action for step in episode.steps] == [*tokens, STOP_ACTION]
    assert episode.steps[-1].done
    assert all(step.observation.action_mask[step.action_id] for step in episode.steps)


def test_p10_collection_uses_only_accepted_evolution_training_records() -> None:
    collection = collect_p10_evolution_rollouts(p10_payload())

    assert [episode.provenance["source_candidate_id"] for episode in collection.episodes] == [
        "evolution_2",
        "evolution_1",
    ]
    assert [episode.final_reward for episode in collection.episodes] == [2.0, 1.0]
    assert all(
        metric["dataset_role"] == "train"
        for episode in collection.episodes
        for metric in episode.training_fold_metrics
    )
    assert collection.failures == ()
    assert collection.data_fingerprint == "training-data-only"


def test_collection_filters_by_training_reward_and_limit() -> None:
    collection = collect_p10_evolution_rollouts(
        p10_payload(),
        minimum_reward=1.5,
        max_episodes=1,
    )
    assert len(collection.episodes) == 1
    assert collection.episodes[0].final_reward == 2.0
