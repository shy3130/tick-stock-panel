from __future__ import annotations

import numpy as np

from research.alphagpt.behavior_clone import (
    BehaviorExample,
    NGramBehaviorPolicy,
    NumpyMaskedTransformer,
    TinyTransformerConfig,
    prepare_reward_conditioned_examples,
)
from research.alphagpt.environment import AlphaEnv
from research.alphagpt.policy import MaskedLogitPolicy, PolicyObservation


def _examples() -> tuple[list[BehaviorExample], list[BehaviorExample]]:
    rows: list[BehaviorExample] = []
    for index in range(80):
        token = index % 4
        rows.append(
            BehaviorExample(
                token_ids=(token,),
                action_mask=(True, True, True, True),
                target_id=token,
                split="train" if index < 60 else "validation",
                episode_id=f"e{index}",
            )
        )
    return rows[:60], rows[60:]


def test_ngram_learns_deterministic_next_token_rule() -> None:
    train, validation = _examples()
    policy = NGramBehaviorPolicy(action_size=4, order=1)
    policy.fit(train)
    metrics = policy.evaluate(validation)
    assert metrics["accuracy"] == 1.0


def test_numpy_transformer_trains_and_checkpoint_round_trips(tmp_path) -> None:
    train, validation = _examples()
    model = NumpyMaskedTransformer(
        TinyTransformerConfig(
            action_size=4,
            max_prefix_length=2,
            d_model=8,
            seed=11,
            learning_rate=0.02,
        )
    )
    result = model.train(train, validation, epochs=40, batch_size=16)
    assert result["final"]["train"]["nll"] < result["initial"]["train"]["nll"]

    path = tmp_path / "model.npz"
    before = model.logits_for_tokens((2,), (True, True, True, True))
    model.save(path)
    restored = NumpyMaskedTransformer.load(path)
    after = restored.logits_for_tokens((2,), (True, True, True, True))
    assert np.allclose(before, after)


def test_transformer_adapter_still_obeys_environment_mask() -> None:
    environment = AlphaEnv()
    observation = PolicyObservation.from_environment(environment)
    model = NumpyMaskedTransformer(
        TinyTransformerConfig(
            action_size=len(environment.action_space),
            max_prefix_length=environment.config.max_formula_length,
            d_model=8,
            seed=3,
        )
    )
    policy = MaskedLogitPolicy(model.logits, seed=3)
    action = policy.select_action(observation)
    action_id = observation.action_space.index(action)
    assert observation.action_mask[action_id]


def test_reward_weighting_prioritizes_high_reward_training_episode() -> None:
    examples = [
        BehaviorExample(
            token_ids=(index,),
            action_mask=(True, True),
            target_id=index,
            split="train",
            episode_id=f"e{index}",
            final_reward=reward,
        )
        for index, reward in enumerate((-2.0, 3.0))
    ]
    weighted, audit = prepare_reward_conditioned_examples(
        examples,
        mode="reward_weighted",
        reward_temperature=1.0,
    )
    assert weighted[1].sample_weight > weighted[0].sample_weight
    assert audit["boundary"] == "training episode rewards only"


def test_elite_filter_uses_episode_reward_and_excludes_low_tail() -> None:
    examples = [
        BehaviorExample(
            token_ids=(index % 2,),
            action_mask=(True, True),
            target_id=index % 2,
            split="train",
            episode_id=f"e{index}",
            final_reward=float(index),
        )
        for index in range(10)
    ]
    elite, audit = prepare_reward_conditioned_examples(
        examples,
        mode="elite",
        elite_quantile=0.60,
    )
    assert len(elite) == 4
    assert min(example.final_reward for example in elite) >= 5.4
    assert audit["selected_episodes"] == 4


def test_weighted_ngram_prefers_high_weight_action_in_same_context() -> None:
    examples = [
        BehaviorExample(
            token_ids=(0,),
            action_mask=(True, True),
            target_id=0,
            split="train",
            episode_id="low",
            final_reward=-2.0,
            sample_weight=0.1,
        ),
        BehaviorExample(
            token_ids=(0,),
            action_mask=(True, True),
            target_id=1,
            split="train",
            episode_id="high",
            final_reward=3.0,
            sample_weight=5.0,
        ),
    ]
    policy = NGramBehaviorPolicy(action_size=2, order=1)
    policy.fit(examples)
    assert int(np.argmax(policy.logits_for_tokens((0,)))) == 1
