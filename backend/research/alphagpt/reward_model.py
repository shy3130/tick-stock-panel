"""公式级训练奖励模型：固定 DSL 特征 + 纯 NumPy ridge。"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from research.alphagpt.environment import AlphaEnv
from research.alphagpt.pool import formula_hash
from research.common.factor_dsl import FEATURE_NAMES, OPS


@dataclass(frozen=True)
class FormulaRewardExample:
    formula_hash: str
    tokens: tuple[str, ...]
    reward: float
    split: str


def load_formula_reward_examples(manifest_path: Path) -> list[FormulaRewardExample]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    examples: list[FormulaRewardExample] = []
    seen_hashes: set[str] = set()
    for episode in manifest["episodes"]:
        folds = episode["training_fold_metrics"]
        if not folds:
            raise ValueError("formula reward source has no training folds")
        if any(
            fold.get("dataset_role") != "train"
            for fold in folds
        ):
            raise ValueError("reward model source contains a non-training fold")
        split = str(episode["split"])
        if split not in {"train", "validation"}:
            raise ValueError(f"unknown formula reward split: {split}")
        tokens = tuple(episode["formula_tokens"])
        digest = str(episode["formula_hash"])
        if formula_hash(tokens) != digest:
            raise ValueError("formula hash does not match normalized tokens")
        if digest in seen_hashes:
            raise ValueError("duplicate formula appears in reward model source")
        seen_hashes.add(digest)
        reward = float(episode["final_reward"])
        if not math.isfinite(reward):
            raise ValueError("formula reward must be finite")
        examples.append(
            FormulaRewardExample(
                formula_hash=digest,
                tokens=tokens,
                reward=reward,
                split=split,
            )
        )
    if not examples:
        raise ValueError("formula reward dataset is empty")
    return examples


@dataclass(frozen=True)
class FormulaFeatureConfig:
    action_space: tuple[str, ...]
    max_formula_length: int
    max_complexity: int


class FormulaFeaturizer:
    """固定、无学习的 token 结构特征，避免 reward model 偷看行情。"""

    def __init__(self, config: FormulaFeatureConfig) -> None:
        if config.max_formula_length <= 0 or config.max_complexity <= 0:
            raise ValueError("formula feature limits must be positive")
        if len(config.action_space) != len(set(config.action_space)):
            raise ValueError("formula feature action space must be unique")
        self.config = config
        self.action_to_id = {
            action: index for index, action in enumerate(config.action_space)
        }
        self.vocab_size = len(config.action_space)
        self.feature_names = self._feature_names()

    def _feature_names(self) -> list[str]:
        names = [
            "length_norm",
            "complexity_norm",
            "max_stack_norm",
            "leaf_ratio",
            "unary_ratio",
            "binary_ratio",
            "ternary_ratio",
            "unique_token_ratio",
        ]
        names.extend(f"unigram:{token}" for token in self.config.action_space)
        names.extend(
            f"bigram:{left}>{right}"
            for left in self.config.action_space
            for right in self.config.action_space
        )
        names.extend(f"first:{token}" for token in self.config.action_space)
        names.extend(f"last:{token}" for token in self.config.action_space)
        return names

    def transform_one(self, tokens: Sequence[str]) -> np.ndarray:
        if not tokens:
            raise ValueError("formula must not be empty")
        try:
            ids = [self.action_to_id[token] for token in tokens]
        except KeyError as exc:
            raise ValueError(f"unknown formula token: {exc.args[0]}") from exc
        length = len(tokens)
        complexity = AlphaEnv.formula_complexity(tokens)
        depth = 0
        max_depth = 0
        arity_counts = {1: 0, 2: 0, 3: 0}
        leaves = 0
        for token in tokens:
            if token in FEATURE_NAMES:
                depth += 1
                leaves += 1
            elif token in OPS:
                arity = int(OPS[token][1])
                depth = depth - arity + 1
                arity_counts[arity] += 1
                if depth < 1:
                    raise ValueError("formula is not valid RPN")
            else:
                raise ValueError(f"formula token is not executable: {token}")
            max_depth = max(max_depth, depth)
        if depth != 1:
            raise ValueError("formula is not valid RPN")

        values = [
            length / self.config.max_formula_length,
            complexity / self.config.max_complexity,
            max_depth / self.config.max_formula_length,
            leaves / length,
            arity_counts[1] / length,
            arity_counts[2] / length,
            arity_counts[3] / length,
            len(set(tokens)) / length,
        ]
        unigrams = np.zeros(self.vocab_size, dtype=float)
        for token_id in ids:
            unigrams[token_id] += 1.0 / length
        bigrams = np.zeros((self.vocab_size, self.vocab_size), dtype=float)
        denominator = max(1, length - 1)
        for left, right in zip(ids, ids[1:]):
            bigrams[left, right] += 1.0 / denominator
        first = np.zeros(self.vocab_size, dtype=float)
        last = np.zeros(self.vocab_size, dtype=float)
        first[ids[0]] = 1.0
        last[ids[-1]] = 1.0
        return np.concatenate(
            [
                np.asarray(values, dtype=float),
                unigrams,
                bigrams.reshape(-1),
                first,
                last,
            ]
        )

    def transform(self, examples: Sequence[FormulaRewardExample]) -> np.ndarray:
        return np.vstack([self.transform_one(example.tokens) for example in examples])


class RidgeRewardModel:
    """使用 dual closed form 的标准化 ridge；适合 p >> n 的小样本。"""

    def __init__(self, *, alpha: float) -> None:
        if alpha <= 0 or not math.isfinite(alpha):
            raise ValueError("alpha must be finite and > 0")
        self.alpha = float(alpha)
        self.feature_mean: np.ndarray | None = None
        self.feature_scale: np.ndarray | None = None
        self.target_mean = 0.0
        self.weights: np.ndarray | None = None

    def fit(self, features: np.ndarray, targets: np.ndarray) -> RidgeRewardModel:
        x = np.asarray(features, dtype=float)
        y = np.asarray(targets, dtype=float)
        if x.ndim != 2 or y.shape != (len(x),):
            raise ValueError("invalid feature/target shape")
        self.feature_mean = x.mean(axis=0)
        scale = x.std(axis=0)
        self.feature_scale = np.where(scale > 1e-12, scale, 1.0)
        standardized = (x - self.feature_mean) / self.feature_scale
        self.target_mean = float(y.mean())
        centered = y - self.target_mean
        kernel = standardized @ standardized.T
        dual = np.linalg.solve(
            kernel + self.alpha * np.eye(len(kernel)),
            centered,
        )
        self.weights = standardized.T @ dual
        return self

    def predict(self, features: np.ndarray) -> np.ndarray:
        if self.weights is None or self.feature_mean is None or self.feature_scale is None:
            raise RuntimeError("model is not fitted")
        x = np.asarray(features, dtype=float)
        return (x - self.feature_mean) / self.feature_scale @ self.weights + self.target_mean

    def save(self, path: Path, *, feature_config: FormulaFeatureConfig) -> None:
        if self.weights is None or self.feature_mean is None or self.feature_scale is None:
            raise RuntimeError("model is not fitted")
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "alpha": self.alpha,
            "target_mean": self.target_mean,
            "feature_config": {
                **asdict(feature_config),
                "action_space": list(feature_config.action_space),
            },
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("wb") as handle:
            np.savez_compressed(
                handle,
                metadata=np.array(json.dumps(metadata, sort_keys=True)),
                feature_mean=self.feature_mean,
                feature_scale=self.feature_scale,
                weights=self.weights,
            )
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path) -> tuple[RidgeRewardModel, FormulaFeatureConfig]:
        with np.load(path, allow_pickle=False) as archive:
            metadata = json.loads(str(archive["metadata"]))
            model = cls(alpha=float(metadata["alpha"]))
            model.target_mean = float(metadata["target_mean"])
            model.feature_mean = archive["feature_mean"].copy()
            model.feature_scale = archive["feature_scale"].copy()
            model.weights = archive["weights"].copy()
        feature_data = metadata["feature_config"]
        feature_config = FormulaFeatureConfig(
            action_space=tuple(feature_data["action_space"]),
            max_formula_length=int(feature_data["max_formula_length"]),
            max_complexity=int(feature_data["max_complexity"]),
        )
        return model, feature_config


def rankdata(values: Sequence[float]) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    order = np.argsort(array, kind="mergesort")
    ranks = np.empty(len(array), dtype=float)
    start = 0
    while start < len(array):
        end = start + 1
        while end < len(array) and array[order[end]] == array[order[start]]:
            end += 1
        ranks[order[start:end]] = (start + end - 1) / 2.0 + 1.0
        start = end
    return ranks


def spearman_correlation(left: Sequence[float], right: Sequence[float]) -> float:
    x = rankdata(left)
    y = rankdata(right)
    if float(np.std(x)) <= 1e-12 or float(np.std(y)) <= 1e-12:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def regression_metrics(
    actual: Sequence[float],
    predicted: Sequence[float],
) -> dict[str, float]:
    y = np.asarray(actual, dtype=float)
    p = np.asarray(predicted, dtype=float)
    error = p - y
    pearson = (
        float(np.corrcoef(y, p)[0, 1])
        if float(np.std(y)) > 1e-12 and float(np.std(p)) > 1e-12
        else 0.0
    )
    return {
        "spearman": spearman_correlation(y, p),
        "pearson": pearson,
        "mae": float(np.mean(np.abs(error))),
        "rmse": float(np.sqrt(np.mean(error**2))),
        "sign_accuracy": float(np.mean((p > 0) == (y > 0))),
    }


def top_k_metrics(
    actual: Sequence[float],
    predicted: Sequence[float],
    *,
    fraction: float = 0.20,
) -> dict[str, float | int]:
    if not 0.0 < fraction <= 1.0:
        raise ValueError("fraction must be within (0, 1]")
    y = np.asarray(actual, dtype=float)
    p = np.asarray(predicted, dtype=float)
    count = max(1, int(math.ceil(len(y) * fraction)))
    selected = np.argsort(p, kind="mergesort")[-count:]
    oracle = np.argsort(y, kind="mergesort")[-count:]
    selected_mean = float(np.mean(y[selected]))
    overall_mean = float(np.mean(y))
    return {
        "fraction": fraction,
        "count": count,
        "selected_actual_mean": selected_mean,
        "overall_actual_mean": overall_mean,
        "absolute_lift": selected_mean - overall_mean,
        "oracle_actual_mean": float(np.mean(y[oracle])),
        "selected_positive_ratio": float(np.mean(y[selected] > 0)),
    }


def calibration_bins(
    actual: Sequence[float],
    predicted: Sequence[float],
    *,
    n_bins: int = 4,
) -> list[dict[str, float | int]]:
    y = np.asarray(actual, dtype=float)
    p = np.asarray(predicted, dtype=float)
    groups = np.array_split(np.argsort(p, kind="mergesort"), min(n_bins, len(y)))
    return [
        {
            "n": len(indices),
            "predicted_mean": float(np.mean(p[indices])),
            "actual_mean": float(np.mean(y[indices])),
        }
        for indices in groups
        if len(indices)
    ]


def select_alpha_by_training_cv(
    examples: Sequence[FormulaRewardExample],
    features: np.ndarray,
    *,
    alphas: Sequence[float],
    n_folds: int = 4,
) -> dict[str, Any]:
    if len(examples) != len(features):
        raise ValueError("example/feature length mismatch")
    if n_folds < 2 or n_folds >= len(examples):
        raise ValueError("n_folds must be within [2, n_examples)")
    if not alphas:
        raise ValueError("at least one alpha candidate is required")
    if any(alpha <= 0 or not math.isfinite(alpha) for alpha in alphas):
        raise ValueError("all alpha candidates must be finite and > 0")
    fold_ids = np.asarray(
        [int(example.formula_hash[:16], 16) % n_folds for example in examples],
        dtype=int,
    )
    targets = np.asarray([example.reward for example in examples], dtype=float)
    results: list[dict[str, float]] = []
    for alpha in alphas:
        predictions = np.full(len(examples), np.nan)
        for fold in range(n_folds):
            validation = fold_ids == fold
            training = ~validation
            if not np.any(validation) or not np.any(training):
                raise ValueError("training CV produced an empty fold")
            model = RidgeRewardModel(alpha=float(alpha)).fit(
                features[training],
                targets[training],
            )
            predictions[validation] = model.predict(features[validation])
        metrics = regression_metrics(targets, predictions)
        results.append({"alpha": float(alpha), **metrics})
    selected = max(results, key=lambda item: (item["spearman"], -item["rmse"]))
    return {
        "fold_rule": "first 64 formula-hash bits modulo n_folds",
        "n_folds": n_folds,
        "candidates": results,
        "selected_alpha": selected["alpha"],
        "selection_rule": "max OOF Spearman, then min OOF RMSE",
    }
