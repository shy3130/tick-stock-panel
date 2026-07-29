"""只依赖训练折指标的 AlphaGPT v1 稳健奖励。"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Sequence


@dataclass(frozen=True)
class TrainingFoldMetrics:
    """搜索阶段可见的单个训练折指标。

    ``dataset_role`` 被固定为 ``train``。holdout/test 指标使用其他结构保存，
    不能传入奖励函数。
    """

    fold_id: str
    start: str
    end: str
    mean_ic: float
    icir: float
    total_return: float
    turnover: float
    top_decile_sharpe: float
    dataset_role: str = "train"

    def __post_init__(self) -> None:
        if self.dataset_role != "train":
            raise ValueError("RobustReward accepts training folds only")
        numeric = (
            self.mean_ic,
            self.icir,
            self.total_return,
            self.turnover,
            self.top_decile_sharpe,
        )
        if not all(math.isfinite(float(value)) for value in numeric):
            raise ValueError(f"non-finite metric in training fold {self.fold_id}")
        if self.turnover < 0:
            raise ValueError("turnover must be >= 0")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RobustRewardConfig:
    median_icir_weight: float = 1.0
    positive_fold_weight: float = 0.50
    stability_weight: float = 0.25
    turnover_penalty_weight: float = 0.10
    complexity_penalty_weight: float = 0.20
    fold_variance_penalty_weight: float = 0.25
    correlation_penalty_weight: float = 0.25
    max_complexity: int = 20

    def __post_init__(self) -> None:
        if self.max_complexity <= 0:
            raise ValueError("max_complexity must be > 0")


@dataclass(frozen=True)
class RewardBreakdown:
    total: float
    median_icir: float
    positive_return_fold_ratio: float
    stability: float
    median_turnover: float
    normalized_complexity: float
    fold_icir_variance: float
    max_abs_correlation: float
    positive_components: dict[str, float]
    penalties: dict[str, float]
    formula: str
    training_fold_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["training_fold_ids"] = list(self.training_fold_ids)
        return data


class RobustReward:
    """可审计的训练折奖励；接口不存在 test/holdout 参数。"""

    FORMULA = (
        "w_icir*median(train_icir) + w_pos*positive_train_return_ratio "
        "+ w_stability/(1+std(train_icir)) "
        "- w_turnover*median(train_turnover) "
        "- w_complexity*(complexity/max_complexity) "
        "- w_variance*var(train_icir) "
        "- w_correlation*max_abs_train_factor_correlation"
    )

    def __init__(self, config: RobustRewardConfig | None = None) -> None:
        self.config = config or RobustRewardConfig()

    def score(
        self,
        training_folds: Sequence[TrainingFoldMetrics],
        *,
        complexity: int,
        max_abs_correlation: float = 0.0,
    ) -> RewardBreakdown:
        if not training_folds:
            raise ValueError("at least one training fold is required")
        if not all(isinstance(fold, TrainingFoldMetrics) for fold in training_folds):
            raise TypeError("training_folds must contain TrainingFoldMetrics only")
        if any(fold.dataset_role != "train" for fold in training_folds):
            raise ValueError("test/holdout folds must never enter RobustReward")
        if complexity < 0:
            raise ValueError("complexity must be >= 0")
        if not math.isfinite(max_abs_correlation):
            raise ValueError("max_abs_correlation must be finite")

        icirs = [float(fold.icir) for fold in training_folds]
        returns = [float(fold.total_return) for fold in training_folds]
        turnovers = [float(fold.turnover) for fold in training_folds]
        median_icir = float(statistics.median(icirs))
        positive_ratio = sum(value > 0 for value in returns) / len(returns)
        icir_std = float(statistics.pstdev(icirs))
        stability = 1.0 / (1.0 + icir_std)
        median_turnover = float(statistics.median(turnovers))
        normalized_complexity = min(
            1.0,
            float(complexity) / float(self.config.max_complexity),
        )
        fold_variance = float(statistics.pvariance(icirs))
        correlation = min(1.0, abs(float(max_abs_correlation)))

        positive_components = {
            "median_icir": self.config.median_icir_weight * median_icir,
            "positive_return_folds": self.config.positive_fold_weight * positive_ratio,
            "stability": self.config.stability_weight * stability,
        }
        penalties = {
            "turnover": self.config.turnover_penalty_weight * median_turnover,
            "complexity": self.config.complexity_penalty_weight * normalized_complexity,
            "fold_variance": self.config.fold_variance_penalty_weight * fold_variance,
            "correlation": self.config.correlation_penalty_weight * correlation,
        }
        total = sum(positive_components.values()) - sum(penalties.values())
        if not math.isfinite(total):
            raise ValueError("reward is non-finite")
        return RewardBreakdown(
            total=float(total),
            median_icir=median_icir,
            positive_return_fold_ratio=float(positive_ratio),
            stability=stability,
            median_turnover=median_turnover,
            normalized_complexity=normalized_complexity,
            fold_icir_variance=fold_variance,
            max_abs_correlation=correlation,
            positive_components=positive_components,
            penalties=penalties,
            formula=self.FORMULA,
            training_fold_ids=tuple(fold.fold_id for fold in training_folds),
        )

    def definition(self) -> dict[str, object]:
        return {
            "formula": self.FORMULA,
            "weights": asdict(self.config),
            "data_boundary": "training folds only; holdout/test is not an input",
        }
