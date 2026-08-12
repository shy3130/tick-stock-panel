"""确定性进化公式搜索及同预算随机基线。"""

from __future__ import annotations

import ast
import json
import random
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import numpy as np

from research.alphagpt.environment import AlphaEnv, AlphaEnvConfig
from research.alphagpt.pool import FactorCandidate, FactorPool, formula_hash
from research.alphagpt.reward import RobustReward, TrainingFoldMetrics
from research.common.factor_dsl import FEATURE_NAMES, OPS


class CandidateEvaluationError(RuntimeError):
    """候选无法产生可信训练指标。"""


@dataclass(frozen=True)
class EvaluationOutcome:
    training_folds: tuple[TrainingFoldMetrics, ...]
    correlation_signal: np.ndarray
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        signal = np.asarray(self.correlation_signal, dtype=float).reshape(-1)
        if signal.size < 3:
            raise ValueError("correlation signal must contain at least 3 observations")
        if not np.all(np.isfinite(signal)):
            raise ValueError("correlation signal contains NaN or infinity")
        object.__setattr__(self, "correlation_signal", signal)


Evaluator = Callable[[Sequence[str]], EvaluationOutcome]


@dataclass(frozen=True)
class SearchConfig:
    candidate_budget: int = 40
    population_size: int = 10
    elite_size: int = 4
    crossover_probability: float = 0.45
    correlation_threshold: float = 0.95
    final_candidate_count: int = 5
    seed: int = 20260723
    data_fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.candidate_budget <= 0:
            raise ValueError("candidate_budget must be > 0")
        if self.population_size <= 0:
            raise ValueError("population_size must be > 0")
        if not 1 <= self.elite_size <= self.population_size:
            raise ValueError("elite_size must be within population size")
        if not 0.0 <= self.crossover_probability <= 1.0:
            raise ValueError("crossover_probability must be within [0, 1]")
        if not 0.0 <= self.correlation_threshold <= 1.0:
            raise ValueError("correlation_threshold must be within [0, 1]")
        if self.final_candidate_count <= 0:
            raise ValueError("final_candidate_count must be > 0")


@dataclass
class SearchResult:
    method: str
    seed: int
    evaluation_budget: int
    evaluations_used: int
    generation_attempts: int
    completed: bool
    pool: FactorPool
    final_candidates: list[FactorCandidate]

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "seed": self.seed,
            "evaluation_budget": self.evaluation_budget,
            "evaluations_used": self.evaluations_used,
            "generation_attempts": self.generation_attempts,
            "completed": self.completed,
            "pool": self.pool.to_dict(),
            "final_candidates": [candidate.to_dict() for candidate in self.final_candidates],
        }


@dataclass
class _Node:
    token: str
    children: list[_Node] = field(default_factory=list)


def _parse_rpn(tokens: Sequence[str]) -> _Node:
    stack: list[_Node] = []
    for token in tokens:
        if token in FEATURE_NAMES:
            stack.append(_Node(token))
            continue
        if token not in OPS:
            raise ValueError(f"unknown token: {token}")
        arity = int(OPS[token][1])
        if len(stack) < arity:
            raise ValueError("stack underflow")
        children = stack[-arity:]
        del stack[-arity:]
        stack.append(_Node(token, children))
    if len(stack) != 1:
        raise ValueError("formula does not reduce to one stack item")
    return stack[0]


def _to_rpn(node: _Node) -> list[str]:
    tokens: list[str] = []
    for child in node.children:
        tokens.extend(_to_rpn(child))
    tokens.append(node.token)
    return tokens


def _clone(node: _Node) -> _Node:
    return _Node(node.token, [_clone(child) for child in node.children])


def _paths(node: _Node, prefix: tuple[int, ...] = ()) -> list[tuple[int, ...]]:
    paths = [prefix]
    for index, child in enumerate(node.children):
        paths.extend(_paths(child, (*prefix, index)))
    return paths


def _subtree(node: _Node, path: tuple[int, ...]) -> _Node:
    current = node
    for index in path:
        current = current.children[index]
    return current


def _replace_subtree(node: _Node, path: tuple[int, ...], replacement: _Node) -> _Node:
    if not path:
        return _clone(replacement)
    root = _clone(node)
    parent = _subtree(root, path[:-1])
    parent.children[path[-1]] = _clone(replacement)
    return root


def mutate_formula(
    tokens: Sequence[str],
    *,
    rng: random.Random,
    environment: AlphaEnv,
) -> list[str]:
    """同元数 token 替换，保持 RPN 结构有效。"""

    source = list(tokens)
    for _ in range(64):
        mutated = source.copy()
        index = rng.randrange(len(mutated))
        old = mutated[index]
        if old in FEATURE_NAMES:
            choices = [token for token in FEATURE_NAMES if token != old]
        else:
            arity = int(OPS[old][1])
            choices = [
                token
                for token, (_, token_arity) in OPS.items()
                if token_arity == arity and token != old
            ]
        if not choices:
            continue
        mutated[index] = rng.choice(choices)
        if mutated != source and environment.validate_formula(mutated):
            return mutated
    return environment.sample_formula()


def crossover_formulas(
    left: Sequence[str],
    right: Sequence[str],
    *,
    rng: random.Random,
    environment: AlphaEnv,
) -> list[str]:
    """RPN 语法树子树交换；超预算时确定性重试。"""

    left_tree = _parse_rpn(left)
    right_tree = _parse_rpn(right)
    left_paths = _paths(left_tree)
    right_paths = _paths(right_tree)
    for _ in range(96):
        left_path = rng.choice(left_paths)
        right_path = rng.choice(right_paths)
        child = _replace_subtree(left_tree, left_path, _subtree(right_tree, right_path))
        tokens = _to_rpn(child)
        if list(tokens) != list(left) and environment.validate_formula(tokens):
            return tokens
    return mutate_formula(left, rng=rng, environment=environment)


class FormulaSearch:
    """随机或进化搜索；每次调用 evaluator 都严格计入候选预算。"""

    def __init__(
        self,
        *,
        method: Literal["random", "evolution"],
        evaluator: Evaluator,
        environment_config: AlphaEnvConfig,
        search_config: SearchConfig,
        reward: RobustReward,
        checkpoint_path: Path | None = None,
    ) -> None:
        self.method = method
        self.evaluator = evaluator
        self.environment_config = replace(environment_config, seed=search_config.seed)
        self.search_config = search_config
        self.reward = reward
        self.checkpoint_path = checkpoint_path
        self.environment = AlphaEnv(self.environment_config)
        self.rng = random.Random(search_config.seed + (0 if method == "random" else 1))
        self.pool = FactorPool(search_config.correlation_threshold)
        self.evaluations_used = 0
        self.generation_attempts = 0
        self.attempted_hashes: set[str] = set()
        self.completed = False

    def _save_checkpoint(self) -> None:
        if self.checkpoint_path is None:
            return
        payload = {
            "schema_version": 1,
            "method": self.method,
            "environment_config": asdict(self.environment_config),
            "search_config": asdict(self.search_config),
            "evaluations_used": self.evaluations_used,
            "generation_attempts": self.generation_attempts,
            "attempted_hashes": sorted(self.attempted_hashes),
            "rng_state": repr(self.rng.getstate()),
            "environment_rng_state": repr(self.environment.get_rng_state()),
            "pool": self.pool.to_dict(include_signals=True),
            "completed": self.completed,
        }
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.checkpoint_path.with_suffix(self.checkpoint_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        temporary.replace(self.checkpoint_path)

    def _load_checkpoint(self) -> bool:
        if self.checkpoint_path is None or not self.checkpoint_path.exists():
            return False
        payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        if payload.get("method") != self.method:
            raise ValueError("checkpoint search method mismatch")
        if payload.get("environment_config") != asdict(self.environment_config):
            raise ValueError("checkpoint environment config mismatch")
        if payload.get("search_config") != asdict(self.search_config):
            raise ValueError("checkpoint search config mismatch")
        self.evaluations_used = int(payload["evaluations_used"])
        self.generation_attempts = int(payload["generation_attempts"])
        self.attempted_hashes = set(payload["attempted_hashes"])
        self.rng.setstate(ast.literal_eval(payload["rng_state"]))
        self.environment.set_rng_state(ast.literal_eval(payload["environment_rng_state"]))
        self.pool = FactorPool.from_dict(payload["pool"])
        self.completed = bool(payload.get("completed", False))
        return True

    def _evaluate(
        self,
        tokens: Sequence[str],
        *,
        generation_method: str,
        parent_formulas: Sequence[str] = (),
    ) -> FactorCandidate | None:
        digest = formula_hash(tokens)
        if digest in self.attempted_hashes:
            self.pool.record_failure(
                reason="duplicate_formula",
                formula=tokens,
                generation_method=generation_method,
                parent_formulas=parent_formulas,
                details={"stage": "pre_evaluation"},
            )
            return None
        self.attempted_hashes.add(digest)
        self.evaluations_used += 1
        candidate_id = f"{self.method}_{self.evaluations_used:06d}"
        try:
            outcome = self.evaluator(tokens)
            correlation = self.pool.max_abs_correlation(outcome.correlation_signal)
            breakdown = self.reward.score(
                outcome.training_folds,
                complexity=self.environment.formula_complexity(tokens),
                max_abs_correlation=correlation[0],
            )
            result = self.pool.add_candidate(
                candidate_id=candidate_id,
                formula=tokens,
                parent_formulas=parent_formulas,
                generation_method=generation_method,
                complexity=self.environment.formula_complexity(tokens),
                fold_metrics=[fold.to_dict() for fold in outcome.training_folds],
                reward=breakdown.to_dict(),
                signal=outcome.correlation_signal,
                correlation=correlation,
            )
            candidate = result.candidate
        except Exception as exc:
            self.pool.record_failure(
                reason=_failure_reason(exc),
                formula=tokens,
                generation_method=generation_method,
                parent_formulas=parent_formulas,
                details={
                    "candidate_id": candidate_id,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            candidate = None
        self._save_checkpoint()
        return candidate

    def _sample_unique(self) -> list[str] | None:
        for _ in range(512):
            self.generation_attempts += 1
            tokens = self.environment.sample_formula()
            if formula_hash(tokens) not in self.attempted_hashes:
                return tokens
            self.pool.record_failure(
                reason="duplicate_formula",
                formula=tokens,
                generation_method=f"{self.method}_generation",
                details={"stage": "generation"},
            )
        return None

    def _run_random(self) -> None:
        while self.evaluations_used < self.search_config.candidate_budget:
            tokens = self._sample_unique()
            if tokens is None:
                raise RuntimeError("unable to generate a unique random candidate")
            self._evaluate(tokens, generation_method="random")

    def _run_evolution(self) -> None:
        initial_budget = min(
            self.search_config.population_size,
            self.search_config.candidate_budget,
        )
        while self.evaluations_used < initial_budget:
            tokens = self._sample_unique()
            if tokens is None:
                raise RuntimeError("unable to initialize evolution population")
            self._evaluate(tokens, generation_method="evolution_initial")

        while self.evaluations_used < self.search_config.candidate_budget:
            elites = self.pool.ranked_candidates()[: self.search_config.elite_size]
            self.generation_attempts += 1
            if not elites:
                tokens = self._sample_unique()
                if tokens is None:
                    raise RuntimeError("evolution has no valid candidates")
                method = "evolution_random_fallback"
                parents: list[str] = []
            elif len(elites) >= 2 and self.rng.random() < self.search_config.crossover_probability:
                left, right = self.rng.sample(elites, 2)
                tokens = crossover_formulas(
                    left.tokens,
                    right.tokens,
                    rng=self.rng,
                    environment=self.environment,
                )
                method = "crossover"
                parents = [left.formula, right.formula]
            else:
                parent = self.rng.choice(elites)
                tokens = mutate_formula(
                    parent.tokens,
                    rng=self.rng,
                    environment=self.environment,
                )
                method = "mutation"
                parents = [parent.formula]

            if formula_hash(tokens) in self.attempted_hashes:
                self.pool.record_failure(
                    reason="duplicate_formula",
                    formula=tokens,
                    generation_method=method,
                    parent_formulas=parents,
                    details={"stage": "generation"},
                )
                continue
            self._evaluate(tokens, generation_method=method, parent_formulas=parents)

    def run(self, *, resume: bool = False) -> SearchResult:
        if resume:
            self._load_checkpoint()
        if self.evaluations_used > self.search_config.candidate_budget:
            raise ValueError("checkpoint exceeds configured candidate budget")
        if self.evaluations_used < self.search_config.candidate_budget:
            if self.method == "random":
                self._run_random()
            else:
                self._run_evolution()
        self.completed = self.evaluations_used == self.search_config.candidate_budget
        self._save_checkpoint()
        final_candidates = self.pool.ranked_candidates()[
            : self.search_config.final_candidate_count
        ]
        return SearchResult(
            method=self.method,
            seed=self.search_config.seed,
            evaluation_budget=self.search_config.candidate_budget,
            evaluations_used=self.evaluations_used,
            generation_attempts=self.generation_attempts,
            completed=self.completed,
            pool=self.pool,
            final_candidates=final_candidates,
        )


def run_search_comparison(
    *,
    evaluator: Evaluator,
    environment_config: AlphaEnvConfig,
    search_config: SearchConfig,
    reward: RobustReward,
    checkpoint_dir: Path | None = None,
    resume: bool = False,
) -> dict[str, SearchResult]:
    """用完全相同的 evaluator 调用预算运行随机与进化搜索。"""

    results: dict[str, SearchResult] = {}
    for method in ("random", "evolution"):
        checkpoint = (
            checkpoint_dir / f"alphagpt_v1_{method}.checkpoint.json"
            if checkpoint_dir is not None
            else None
        )
        search = FormulaSearch(
            method=method,
            evaluator=evaluator,
            environment_config=environment_config,
            search_config=search_config,
            reward=reward,
            checkpoint_path=checkpoint,
        )
        results[method] = search.run(resume=resume)
    budgets = {
        (result.evaluation_budget, result.evaluations_used)
        for result in results.values()
    }
    if budgets != {(search_config.candidate_budget, search_config.candidate_budget)}:
        raise AssertionError("random and evolution did not use the same evaluation budget")
    return results


def _failure_reason(exc: Exception) -> str:
    message = str(exc).lower()
    if "nan" in message or "non-finite" in message or "infinity" in message:
        return "non_finite"
    if "no signal" in message or "constant signal" in message:
        return "no_signal"
    if isinstance(exc, CandidateEvaluationError):
        return "evaluation_error"
    return "execution_exception"
