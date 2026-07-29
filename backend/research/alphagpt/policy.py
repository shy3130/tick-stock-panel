"""AlphaGPT token policy 的稳定适配层。

本模块不依赖 torch。后续 Transformer 只需提供 logits callable，所有策略都必须
经过同一 action mask，不能绕过 ``AlphaEnv`` 的语法约束。
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from research.alphagpt.environment import STOP_ACTION, AlphaEnv


class PolicyActionError(ValueError):
    """策略返回了未知或被 mask 禁止的动作。"""


@dataclass(frozen=True)
class PolicyObservation:
    """与具体模型框架无关的 token 决策观测。"""

    tokens: tuple[str, ...]
    token_ids: tuple[int, ...]
    stack_depth: int
    complexity: int
    step_index: int
    remaining_formula_tokens: int
    remaining_complexity: int
    action_space: tuple[str, ...]
    action_mask: tuple[bool, ...]

    @classmethod
    def from_environment(cls, environment: AlphaEnv) -> PolicyObservation:
        action_to_id = {
            action: index for index, action in enumerate(environment.action_space)
        }
        return cls(
            tokens=tuple(environment.tokens),
            token_ids=tuple(action_to_id[token] for token in environment.tokens),
            stack_depth=environment.stack_depth,
            complexity=environment.complexity,
            step_index=len(environment.tokens),
            remaining_formula_tokens=(
                environment.config.max_formula_length - len(environment.tokens)
            ),
            remaining_complexity=environment.config.max_complexity - environment.complexity,
            action_space=environment.action_space,
            action_mask=environment.action_mask(),
        )

    @property
    def valid_actions(self) -> tuple[str, ...]:
        return tuple(
            action
            for action, allowed in zip(
                self.action_space,
                self.action_mask,
                strict=True,
            )
            if allowed
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "tokens": list(self.tokens),
            "token_ids": list(self.token_ids),
            "stack_depth": self.stack_depth,
            "complexity": self.complexity,
            "step_index": self.step_index,
            "remaining_formula_tokens": self.remaining_formula_tokens,
            "remaining_complexity": self.remaining_complexity,
            "action_mask": list(self.action_mask),
        }


class TokenPolicy(Protocol):
    """random、replay 和未来学习策略共享的最小契约。"""

    @property
    def name(self) -> str: ...

    def reset(self, *, seed: int | None = None) -> None: ...

    def select_action(self, observation: PolicyObservation) -> str: ...


def masked_logits(
    logits: Sequence[float] | np.ndarray,
    action_mask: Sequence[bool],
) -> np.ndarray:
    """把非法动作置为 ``-inf``，并显式拒绝无合法动作或非有限合法 logits。"""

    values = np.asarray(logits, dtype=float).reshape(-1)
    mask = np.asarray(action_mask, dtype=bool).reshape(-1)
    if values.shape != mask.shape:
        raise ValueError(
            f"logits/action_mask shape mismatch: {values.shape} != {mask.shape}"
        )
    if not np.any(mask):
        raise PolicyActionError("action mask contains no legal action")
    if not np.all(np.isfinite(values[mask])):
        raise PolicyActionError("legal action logits contain NaN or infinity")
    result = values.copy()
    result[~mask] = -np.inf
    return result


def validate_policy_action(observation: PolicyObservation, action: str) -> int:
    """中央校验点：任何 policy 都不能直接向环境提交非法动作。"""

    try:
        action_id = observation.action_space.index(action)
    except ValueError as exc:
        raise PolicyActionError(f"policy returned unknown action: {action}") from exc
    if not observation.action_mask[action_id]:
        raise PolicyActionError(
            f"policy returned masked action {action!r} at step {observation.step_index}"
        )
    return action_id


class RandomTokenPolicy:
    """与 AlphaEnv 采样纪律一致的确定性随机策略。"""

    def __init__(self, *, seed: int, stop_probability: float = 0.18) -> None:
        if not 0.0 <= stop_probability <= 1.0:
            raise ValueError("stop_probability must be within [0, 1]")
        self._seed = seed
        self.stop_probability = stop_probability
        self._rng = random.Random(seed)

    @property
    def name(self) -> str:
        return "random"

    def reset(self, *, seed: int | None = None) -> None:
        self._rng.seed(self._seed if seed is None else seed)

    def select_action(self, observation: PolicyObservation) -> str:
        valid = observation.valid_actions
        if not valid:
            raise PolicyActionError("observation has no legal action")
        non_stop = [action for action in valid if action != STOP_ACTION]
        if STOP_ACTION in valid and (
            not non_stop or self._rng.random() < self.stop_probability
        ):
            return STOP_ACTION
        return self._rng.choice(non_stop)


class ReplayTokenPolicy:
    """把既有公式重放成逐 token 教师轨迹。"""

    def __init__(self, actions: Sequence[str], *, name: str = "teacher_replay") -> None:
        self._actions = tuple(actions)
        self._name = name
        self._index = 0

    @property
    def name(self) -> str:
        return self._name

    def reset(self, *, seed: int | None = None) -> None:
        del seed
        self._index = 0

    def select_action(self, observation: PolicyObservation) -> str:
        del observation
        if self._index >= len(self._actions):
            raise PolicyActionError("replay policy exhausted before episode termination")
        action = self._actions[self._index]
        self._index += 1
        return action


class MaskedLogitPolicy:
    """未来 Transformer/MLP 的无框架 adapter。

    ``logit_fn`` 接收 ``PolicyObservation``，返回与 action space 等长的 logits。
    ``temperature=0`` 使用确定性 argmax；正温度使用种子固定的 softmax 采样。
    """

    def __init__(
        self,
        logit_fn: Callable[[PolicyObservation], Sequence[float] | np.ndarray],
        *,
        seed: int,
        temperature: float = 0.0,
        name: str = "masked_logits",
    ) -> None:
        if temperature < 0 or not math.isfinite(temperature):
            raise ValueError("temperature must be finite and >= 0")
        self.logit_fn = logit_fn
        self._seed = seed
        self.temperature = temperature
        self._name = name
        self._rng = random.Random(seed)

    @property
    def name(self) -> str:
        return self._name

    def reset(self, *, seed: int | None = None) -> None:
        self._rng.seed(self._seed if seed is None else seed)

    def select_action(self, observation: PolicyObservation) -> str:
        values = masked_logits(self.logit_fn(observation), observation.action_mask)
        if self.temperature == 0.0:
            return observation.action_space[int(np.argmax(values))]

        valid_indices = np.flatnonzero(np.isfinite(values))
        scaled = values[valid_indices] / self.temperature
        scaled -= float(np.max(scaled))
        probabilities = np.exp(scaled)
        probabilities /= probabilities.sum()
        threshold = self._rng.random()
        cumulative = 0.0
        for index, probability in zip(valid_indices, probabilities, strict=True):
            cumulative += float(probability)
            if threshold <= cumulative:
                return observation.action_space[int(index)]
        return observation.action_space[int(valid_indices[-1])]
