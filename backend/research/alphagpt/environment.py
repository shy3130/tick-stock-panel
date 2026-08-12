"""AlphaGPT v1 的逐 token 因子公式环境。

环境只负责语法与预算约束，不接触任何训练或测试行情。动作空间由现有
``factor_dsl`` 的特征和算子组成，并额外提供 ``<STOP>`` 终止动作。
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from research.common.factor_dsl import FEATURE_NAMES, OPS, StackVM

STOP_ACTION = "<STOP>"


@dataclass(frozen=True)
class AlphaEnvConfig:
    """公式环境的确定性约束。"""

    max_formula_length: int = 10
    max_complexity: int = 20
    min_formula_length: int = 1
    stop_probability: float = 0.18
    seed: int = 20260723

    def __post_init__(self) -> None:
        if self.max_formula_length < 1:
            raise ValueError("max_formula_length must be >= 1")
        if self.max_complexity < 1:
            raise ValueError("max_complexity must be >= 1")
        if not 1 <= self.min_formula_length <= self.max_formula_length:
            raise ValueError("min_formula_length must be within formula length limits")
        if not 0.0 <= self.stop_probability <= 1.0:
            raise ValueError("stop_probability must be within [0, 1]")


class AlphaEnv:
    """将 RPN 公式生成建模为带合法动作掩码的逐 token 决策过程。"""

    def __init__(self, config: AlphaEnvConfig | None = None) -> None:
        self.config = config or AlphaEnvConfig()
        self.action_space = tuple(FEATURE_NAMES) + tuple(OPS) + (STOP_ACTION,)
        self._rng = random.Random(self.config.seed)
        self._vm = StackVM()
        self.reset()

    @staticmethod
    def token_complexity(token: str) -> int:
        """复杂度：叶子为 1，算子按元数计费。"""

        if token in FEATURE_NAMES:
            return 1
        if token in OPS:
            return int(OPS[token][1])
        raise ValueError(f"unknown token: {token}")

    @classmethod
    def formula_complexity(cls, tokens: Sequence[str]) -> int:
        return sum(cls.token_complexity(token) for token in tokens)

    @staticmethod
    def _minimum_closure_tokens(stack_depth: int) -> int:
        """从当前栈深归约到 1 所需的最少 token 数。

        三元算子一次可把栈深减少 2，二元算子减少 1。
        """

        reductions = max(0, stack_depth - 1)
        return math.ceil(reductions / 2)

    @staticmethod
    def _minimum_closure_complexity(stack_depth: int) -> int:
        reductions = max(0, stack_depth - 1)
        return 3 * (reductions // 2) + 2 * (reductions % 2)

    def reset(self, *, seed: int | None = None) -> tuple[str, ...]:
        """清空当前 episode；仅显式传 seed 时重置 RNG。"""

        if seed is not None:
            self._rng.seed(seed)
        self.tokens: list[str] = []
        self.stack_depth = 0
        self.complexity = 0
        self.terminated = False
        return tuple(self.tokens)

    def get_rng_state(self) -> object:
        """供断点文件保存确定性随机状态。"""

        return self._rng.getstate()

    def set_rng_state(self, state: object) -> None:
        self._rng.setstate(state)  # type: ignore[arg-type]

    def _next_stack_depth(self, token: str) -> int | None:
        if token in FEATURE_NAMES:
            return self.stack_depth + 1
        if token in OPS:
            arity = int(OPS[token][1])
            if self.stack_depth < arity:
                return None
            return self.stack_depth - arity + 1
        return None

    def _can_finish_after(self, token: str) -> bool:
        next_depth = self._next_stack_depth(token)
        if next_depth is None:
            return False
        next_length = len(self.tokens) + 1
        next_complexity = self.complexity + self.token_complexity(token)
        remaining_tokens = self.config.max_formula_length - next_length
        remaining_complexity = self.config.max_complexity - next_complexity
        return (
            next_length <= self.config.max_formula_length
            and next_complexity <= self.config.max_complexity
            and self._minimum_closure_tokens(next_depth) <= remaining_tokens
            and self._minimum_closure_complexity(next_depth) <= remaining_complexity
        )

    def action_mask(self) -> tuple[bool, ...]:
        """返回与 ``action_space`` 对齐的合法动作掩码。"""

        if self.terminated:
            return tuple(False for _ in self.action_space)
        mask = [self._can_finish_after(token) for token in self.action_space[:-1]]
        can_stop = (
            self.stack_depth == 1
            and len(self.tokens) >= self.config.min_formula_length
            and self.validate_formula(self.tokens)
        )
        mask.append(can_stop)
        return tuple(mask)

    def valid_actions(self) -> tuple[str, ...]:
        return tuple(
            action
            for action, is_valid in zip(self.action_space, self.action_mask(), strict=True)
            if is_valid
        )

    def step(self, action: str) -> tuple[tuple[str, ...], bool]:
        """执行一个动作，非法动作立即抛错而不是静默修复。"""

        if self.terminated:
            raise RuntimeError("episode already terminated")
        try:
            action_index = self.action_space.index(action)
        except ValueError as exc:
            raise ValueError(f"unknown action: {action}") from exc
        if not self.action_mask()[action_index]:
            raise ValueError(
                f"illegal action {action!r} at stack={self.stack_depth}, "
                f"length={len(self.tokens)}, complexity={self.complexity}"
            )
        if action == STOP_ACTION:
            self.terminated = True
            return tuple(self.tokens), True

        next_depth = self._next_stack_depth(action)
        if next_depth is None:  # action_mask 已防住，仅保留防御性检查
            raise AssertionError("masked action caused stack underflow")
        self.tokens.append(action)
        self.stack_depth = next_depth
        self.complexity += self.token_complexity(action)
        return tuple(self.tokens), False

    def sample_formula(self) -> list[str]:
        """用环境自己的 RNG 采样一个合法、可由 StackVM 执行的公式。"""

        self.reset()
        while not self.terminated:
            valid = self.valid_actions()
            if not valid:
                raise RuntimeError("action mask reached a dead end")
            non_stop = [action for action in valid if action != STOP_ACTION]
            if STOP_ACTION in valid and (
                not non_stop or self._rng.random() < self.config.stop_probability
            ):
                action = STOP_ACTION
            else:
                action = self._rng.choice(non_stop)
            self.step(action)
        return list(self.tokens)

    def sample_formulas(self, count: int) -> list[list[str]]:
        if count < 0:
            raise ValueError("count must be >= 0")
        return [self.sample_formula() for _ in range(count)]

    def validate_formula(self, tokens: Iterable[str]) -> bool:
        """同时做结构校验和一次 StackVM 烟雾执行。"""

        formula = list(tokens)
        if not self.config.min_formula_length <= len(formula) <= self.config.max_formula_length:
            return False
        try:
            if self.formula_complexity(formula) > self.config.max_complexity:
                return False
        except ValueError:
            return False

        depth = 0
        for token in formula:
            if token in FEATURE_NAMES:
                depth += 1
            elif token in OPS:
                arity = int(OPS[token][1])
                if depth < arity:
                    return False
                depth = depth - arity + 1
            else:
                return False
        if depth != 1:
            return False

        base = np.linspace(0.1, 1.0, 16, dtype=float)
        features = {
            name: base + (index + 1) * 0.01
            for index, name in enumerate(FEATURE_NAMES)
        }
        return self._vm.execute(formula, features) is not None
