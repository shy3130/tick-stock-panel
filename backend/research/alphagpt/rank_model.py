"""公式奖励的纯 NumPy pairwise/listwise 线性排序模型。"""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, Sequence

import numpy as np

from research.alphagpt.reward_model import (
    FormulaFeatureConfig,
    rankdata,
    spearman_correlation,
    top_k_metrics,
)

RankObjective = Literal["pairwise", "listwise"]


def _ridge_weights(features: np.ndarray, targets: np.ndarray, alpha: float) -> np.ndarray:
    """根据 n/p 选择 dual 或 primal closed form。"""

    rows, columns = features.shape
    if rows <= columns:
        dual = np.linalg.solve(
            features @ features.T + alpha * np.eye(rows),
            targets,
        )
        return features.T @ dual
    return np.linalg.solve(
        features.T @ features + alpha * np.eye(columns),
        features.T @ targets,
    )


def _listwise_targets(rewards: np.ndarray, groups: np.ndarray) -> np.ndarray:
    targets = np.zeros(len(rewards), dtype=float)
    for group in sorted(set(groups.tolist())):
        selected = groups == group
        ranks = rankdata(rewards[selected])
        denominator = max(1, len(ranks) - 1)
        targets[selected] = 2.0 * (ranks - 1.0) / denominator - 1.0
    return targets


def _pairwise_dataset(
    features: np.ndarray,
    rewards: np.ndarray,
    groups: np.ndarray,
    *,
    min_reward_gap: float,
    max_pairs_per_group: int,
) -> tuple[np.ndarray, np.ndarray]:
    differences: list[np.ndarray] = []
    targets: list[float] = []
    for group in sorted(set(groups.tolist())):
        indices = np.flatnonzero(groups == group)
        pairs: list[tuple[float, int, int]] = []
        for left_offset, left in enumerate(indices):
            for right in indices[left_offset + 1 :]:
                gap = float(rewards[left] - rewards[right])
                if abs(gap) >= min_reward_gap:
                    pairs.append((abs(gap), int(left), int(right)))
        pairs.sort(key=lambda item: (item[0], item[1], item[2]))
        if len(pairs) > max_pairs_per_group:
            positions = np.linspace(
                0,
                len(pairs) - 1,
                max_pairs_per_group,
                dtype=int,
            )
            pairs = [pairs[position] for position in positions]
        for _, left, right in pairs:
            differences.append(features[left] - features[right])
            targets.append(1.0 if rewards[left] > rewards[right] else -1.0)
    if not differences:
        raise ValueError("pairwise objective produced no eligible reward pairs")
    return np.vstack(differences), np.asarray(targets, dtype=float)


class FormulaRanker:
    def __init__(
        self,
        *,
        objective: RankObjective,
        alpha: float,
        min_reward_gap: float = 0.10,
        max_pairs_per_group: int = 128,
    ) -> None:
        if objective not in {"pairwise", "listwise"}:
            raise ValueError(f"unknown rank objective: {objective}")
        if alpha <= 0 or not math.isfinite(alpha):
            raise ValueError("alpha must be finite and > 0")
        if min_reward_gap < 0 or not math.isfinite(min_reward_gap):
            raise ValueError("min reward gap must be finite and >= 0")
        if max_pairs_per_group <= 0:
            raise ValueError("max pairs per group must be > 0")
        self.objective = objective
        self.alpha = float(alpha)
        self.min_reward_gap = float(min_reward_gap)
        self.max_pairs_per_group = int(max_pairs_per_group)
        self.feature_mean: np.ndarray | None = None
        self.feature_scale: np.ndarray | None = None
        self.weights: np.ndarray | None = None
        self.training_pairs = 0

    def fit(
        self,
        features: np.ndarray,
        rewards: Sequence[float],
        groups: Sequence[int],
    ) -> FormulaRanker:
        x = np.asarray(features, dtype=float)
        y = np.asarray(rewards, dtype=float)
        group_array = np.asarray(groups, dtype=int)
        if x.ndim != 2 or y.shape != (len(x),) or group_array.shape != (len(x),):
            raise ValueError("invalid ranker feature/reward/group shape")
        if len(set(group_array.tolist())) < 2:
            raise ValueError("ranker requires at least two data-seed groups")
        self.feature_mean = x.mean(axis=0)
        scale = x.std(axis=0)
        self.feature_scale = np.where(scale > 1e-12, scale, 1.0)
        standardized = (x - self.feature_mean) / self.feature_scale
        if self.objective == "pairwise":
            training_x, training_y = _pairwise_dataset(
                standardized,
                y,
                group_array,
                min_reward_gap=self.min_reward_gap,
                max_pairs_per_group=self.max_pairs_per_group,
            )
            self.training_pairs = len(training_y)
        else:
            training_x = standardized
            training_y = _listwise_targets(y, group_array)
            self.training_pairs = 0
        self.weights = _ridge_weights(training_x, training_y, self.alpha)
        return self

    def predict(self, features: np.ndarray) -> np.ndarray:
        if self.weights is None or self.feature_mean is None or self.feature_scale is None:
            raise RuntimeError("ranker is not fitted")
        x = np.asarray(features, dtype=float)
        return (x - self.feature_mean) / self.feature_scale @ self.weights

    def save(self, path: Path, *, feature_config: FormulaFeatureConfig) -> None:
        if self.weights is None or self.feature_mean is None or self.feature_scale is None:
            raise RuntimeError("ranker is not fitted")
        metadata = {
            "objective": self.objective,
            "alpha": self.alpha,
            "min_reward_gap": self.min_reward_gap,
            "max_pairs_per_group": self.max_pairs_per_group,
            "training_pairs": self.training_pairs,
            "feature_config": {
                **asdict(feature_config),
                "action_space": list(feature_config.action_space),
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
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
    def load(cls, path: Path) -> tuple[FormulaRanker, FormulaFeatureConfig]:
        with np.load(path, allow_pickle=False) as archive:
            metadata = json.loads(str(archive["metadata"]))
            model = cls(
                objective=metadata["objective"],
                alpha=float(metadata["alpha"]),
                min_reward_gap=float(metadata["min_reward_gap"]),
                max_pairs_per_group=int(metadata["max_pairs_per_group"]),
            )
            model.training_pairs = int(metadata["training_pairs"])
            model.feature_mean = archive["feature_mean"].copy()
            model.feature_scale = archive["feature_scale"].copy()
            model.weights = archive["weights"].copy()
        config = metadata["feature_config"]
        feature_config = FormulaFeatureConfig(
            action_space=tuple(config["action_space"]),
            max_formula_length=int(config["max_formula_length"]),
            max_complexity=int(config["max_complexity"]),
        )
        return model, feature_config


def pairwise_accuracy(actual: Sequence[float], predicted: Sequence[float]) -> float:
    y = np.asarray(actual, dtype=float)
    p = np.asarray(predicted, dtype=float)
    correct = 0
    total = 0
    for left in range(len(y)):
        for right in range(left + 1, len(y)):
            actual_difference = y[left] - y[right]
            predicted_difference = p[left] - p[right]
            if actual_difference == 0 or predicted_difference == 0:
                continue
            total += 1
            correct += int((actual_difference > 0) == (predicted_difference > 0))
    return correct / total if total else 0.0


def select_ranker_by_group_cv(
    features: np.ndarray,
    rewards: Sequence[float],
    groups: Sequence[int],
    *,
    objectives: Sequence[RankObjective],
    alphas: Sequence[float],
    min_reward_gap: float = 0.10,
    max_pairs_per_group: int = 128,
) -> dict[str, Any]:
    x = np.asarray(features, dtype=float)
    y = np.asarray(rewards, dtype=float)
    group_array = np.asarray(groups, dtype=int)
    unique_groups = sorted(set(group_array.tolist()))
    if len(unique_groups) < 3:
        raise ValueError("group CV requires at least three training seeds")
    candidates: list[dict[str, Any]] = []
    for objective in objectives:
        for alpha in alphas:
            fold_metrics: list[dict[str, float | int]] = []
            for held_out in unique_groups:
                validation = group_array == held_out
                training = ~validation
                model = FormulaRanker(
                    objective=objective,
                    alpha=float(alpha),
                    min_reward_gap=min_reward_gap,
                    max_pairs_per_group=max_pairs_per_group,
                ).fit(x[training], y[training], group_array[training])
                predictions = model.predict(x[validation])
                top_k = top_k_metrics(
                    y[validation],
                    predictions,
                    fraction=0.20,
                )
                fold_metrics.append(
                    {
                        "held_out_seed": held_out,
                        "n": int(np.sum(validation)),
                        "spearman": spearman_correlation(
                            y[validation],
                            predictions,
                        ),
                        "pairwise_accuracy": pairwise_accuracy(
                            y[validation],
                            predictions,
                        ),
                        "top_k_lift": float(top_k["absolute_lift"]),
                    }
                )
            candidates.append(
                {
                    "objective": objective,
                    "alpha": float(alpha),
                    "mean_spearman": float(
                        np.mean([fold["spearman"] for fold in fold_metrics])
                    ),
                    "mean_pairwise_accuracy": float(
                        np.mean(
                            [fold["pairwise_accuracy"] for fold in fold_metrics]
                        )
                    ),
                    "mean_top_k_lift": float(
                        np.mean([fold["top_k_lift"] for fold in fold_metrics])
                    ),
                    "folds": fold_metrics,
                }
            )
    selected = max(
        candidates,
        key=lambda item: (
            item["mean_spearman"],
            item["mean_top_k_lift"],
            item["mean_pairwise_accuracy"],
        ),
    )
    return {
        "group_rule": "leave one complete training data seed out",
        "selection_rule": (
            "max mean seed Spearman, then mean top-k lift, then pairwise accuracy"
        ),
        "candidates": candidates,
        "selected_objective": selected["objective"],
        "selected_alpha": selected["alpha"],
    }
