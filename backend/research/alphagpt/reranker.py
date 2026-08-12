"""Reward model 的确定性候选预筛与同候选池随机对照。"""

from __future__ import annotations

import random
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace

from research.alphagpt.environment import AlphaEnv, AlphaEnvConfig
from research.alphagpt.pool import formula_hash
from research.alphagpt.reward_model import (
    FormulaFeaturizer,
    FormulaRewardExample,
    RidgeRewardModel,
)


@dataclass(frozen=True)
class ScoredFormula:
    formula_hash: str
    tokens: tuple[str, ...]
    predicted_reward: float
    slate_rank: int

    def to_dict(self) -> dict[str, object]:
        return {
            "formula_hash": self.formula_hash,
            "formula_tokens": list(self.tokens),
            "formula": " ".join(self.tokens),
            "predicted_reward": self.predicted_reward,
            "slate_rank": self.slate_rank,
        }


def generate_candidate_slate(
    *,
    environment_config: AlphaEnvConfig,
    seed: int,
    count: int,
    excluded_hashes: Collection[str] = (),
) -> list[tuple[str, ...]]:
    """生成固定大小、去重且未出现在训练语料中的合法公式池。"""

    if count <= 0:
        raise ValueError("count must be > 0")
    environment = AlphaEnv(replace(environment_config, seed=seed))
    seen = set(excluded_hashes)
    slate: list[tuple[str, ...]] = []
    attempts = 0
    while len(slate) < count:
        attempts += 1
        if attempts > count * 1000:
            raise RuntimeError("unable to generate enough unique prospective formulas")
        tokens = tuple(environment.sample_formula())
        digest = formula_hash(tokens)
        if digest in seen:
            continue
        seen.add(digest)
        slate.append(tokens)
    return slate


def score_candidate_slate(
    slate: Sequence[Sequence[str]],
    *,
    model: RidgeRewardModel,
    featurizer: FormulaFeaturizer,
) -> list[ScoredFormula]:
    """只依据公式 token 预测训练奖励，不调用行情 evaluator。"""

    examples = [
        FormulaRewardExample(
            formula_hash=formula_hash(tokens),
            tokens=tuple(tokens),
            reward=0.0,
            split="prospective",
        )
        for tokens in slate
    ]
    predictions = model.predict(featurizer.transform(examples))
    ordered = sorted(
        zip(examples, predictions, strict=True),
        key=lambda item: (-float(item[1]), item[0].formula_hash),
    )
    return [
        ScoredFormula(
            formula_hash=example.formula_hash,
            tokens=example.tokens,
            predicted_reward=float(prediction),
            slate_rank=index,
        )
        for index, (example, prediction) in enumerate(ordered, start=1)
    ]


def random_baseline_selection(
    scored_slate: Sequence[ScoredFormula],
    *,
    count: int,
    seed: int,
) -> list[ScoredFormula]:
    if not 0 < count <= len(scored_slate):
        raise ValueError("count must be within candidate slate size")
    rng = random.Random(seed)
    selected_indices = rng.sample(range(len(scored_slate)), count)
    return [scored_slate[index] for index in selected_indices]
