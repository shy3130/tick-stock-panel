"""Pure factor-mining algorithms and callback-driven nested validation."""
from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any, Literal, Protocol

import numpy as np
import polars as pl

logger = logging.getLogger(__name__)

MAX_MINING_FACTORS = 48
MAX_EXISTING_STRATEGIES = 8
MAX_COMBINATION_SIZE = 4
MAX_BEAM_WIDTH = 32
MAX_FINALISTS = 8
MAX_REAL_TRIALS = 256

# Evidence-based promotion gate documented in docs/mining.md. A candidate must
# clear every threshold before it may be published as an independent strategy.
GATE_MIN_VALID_FOLDS = 2
GATE_MIN_POSITIVE_FOLD_RATIO = 2.0 / 3.0
GATE_MIN_OOS_SHARPE = 0.5
GATE_MAX_DRAWDOWN = -0.25
GATE_MIN_TRADES = 60

MiningProfile = Literal["exploratory", "balanced", "strict"]


class JsonDataclassMixin:
    """Provide JSON output without coupling pure models to Pydantic."""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class MiningBudget(JsonDataclassMixin):
    max_factors: int = MAX_MINING_FACTORS
    max_existing_strategies: int = MAX_EXISTING_STRATEGIES
    max_combination_size: int = MAX_COMBINATION_SIZE
    beam_width: int = 16
    max_proxy_trials: int = 256
    max_trials: int = 96

    def __post_init__(self) -> None:
        if not 1 <= self.max_factors <= MAX_MINING_FACTORS:
            raise ValueError(f"max_factors must be between 1 and {MAX_MINING_FACTORS}")
        if not 0 <= self.max_existing_strategies <= MAX_EXISTING_STRATEGIES:
            raise ValueError(
                "max_existing_strategies must be between 0 and "
                f"{MAX_EXISTING_STRATEGIES}"
            )
        if not 1 <= self.max_combination_size <= MAX_COMBINATION_SIZE:
            raise ValueError(
                "max_combination_size must be between 1 and "
                f"{MAX_COMBINATION_SIZE}"
            )
        if not 1 <= self.beam_width <= MAX_BEAM_WIDTH:
            raise ValueError(f"beam_width must be between 1 and {MAX_BEAM_WIDTH}")
        if self.max_proxy_trials <= 0:
            raise ValueError("max_proxy_trials must be positive")
        if not 1 <= self.max_trials <= MAX_REAL_TRIALS:
            raise ValueError(f"max_trials must be between 1 and {MAX_REAL_TRIALS}")

    @classmethod
    def exploratory(cls) -> MiningBudget:
        return cls(
            max_combination_size=3,
            beam_width=8,
            max_proxy_trials=96,
            max_trials=32,
        )

    @classmethod
    def balanced(cls) -> MiningBudget:
        return cls()

    @classmethod
    def strict(cls) -> MiningBudget:
        return cls(beam_width=32, max_proxy_trials=512, max_trials=256)


@dataclass(frozen=True)
class NestedValidationConfig(JsonDataclassMixin):
    outer_train_bars: int = 504
    outer_test_bars: int = 126
    outer_step_bars: int = 63
    inner_train_bars: int = 252
    inner_test_bars: int = 63
    inner_step_bars: int = 63
    purge_bars: int = 30
    embargo_bars: int = 5
    min_train_bars: int = 126

    def __post_init__(self) -> None:
        positive = {
            "outer_train_bars": self.outer_train_bars,
            "outer_test_bars": self.outer_test_bars,
            "outer_step_bars": self.outer_step_bars,
            "inner_train_bars": self.inner_train_bars,
            "inner_test_bars": self.inner_test_bars,
            "inner_step_bars": self.inner_step_bars,
            "min_train_bars": self.min_train_bars,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise ValueError(f"nested validation bars must be positive: {invalid}")
        if self.purge_bars < 0 or self.embargo_bars < 0:
            raise ValueError("purge_bars and embargo_bars must not be negative")
        if self.outer_train_bars < self.min_train_bars:
            raise ValueError("outer_train_bars is smaller than min_train_bars")
        if self.inner_train_bars < self.min_train_bars:
            raise ValueError("inner_train_bars is smaller than min_train_bars")

    @classmethod
    def exploratory(cls) -> NestedValidationConfig:
        return cls(
            outer_train_bars=126,
            outer_test_bars=63,
            outer_step_bars=63,
            inner_train_bars=63,
            inner_test_bars=21,
            inner_step_bars=21,
            purge_bars=30,
            embargo_bars=5,
            min_train_bars=63,
        )

    @classmethod
    def balanced(cls) -> NestedValidationConfig:
        return cls()

    @classmethod
    def strict(cls) -> NestedValidationConfig:
        return cls(
            outer_train_bars=756,
            outer_test_bars=126,
            outer_step_bars=126,
            inner_train_bars=504,
            inner_test_bars=63,
            inner_step_bars=63,
            purge_bars=30,
            embargo_bars=5,
            min_train_bars=126,
        )


def validation_config_for_profile(profile: str) -> NestedValidationConfig:
    if profile not in {"exploratory", "balanced", "strict"}:
        raise ValueError(f"unknown mining profile: {profile}")
    return getattr(NestedValidationConfig, profile)()


def required_outer_folds(profile: str) -> int:
    validation_config_for_profile(profile)
    return 1 if profile == "exploratory" else 3


def required_trading_bars(
    config: NestedValidationConfig,
    outer_folds: int,
) -> int:
    if outer_folds <= 0:
        raise ValueError("outer_folds must be positive")
    return (
        config.outer_train_bars
        + config.purge_bars
        + config.outer_test_bars
        + (outer_folds - 1) * config.outer_step_bars
    )


def nested_fold_count(
    trading_bars: int,
    config: NestedValidationConfig,
) -> int:
    if trading_bars < 0:
        raise ValueError("trading_bars must not be negative")
    one_fold_bars = required_trading_bars(config, 1)
    if trading_bars < one_fold_bars:
        return 0
    return 1 + (trading_bars - one_fold_bars) // config.outer_step_bars


@dataclass(frozen=True)
class CandidateGateResult(JsonDataclassMixin):
    qualified: bool
    reasons: tuple[str, ...]


def evaluate_candidate_gate(
    *,
    confidence: str | None,
    valid_folds: int | None,
    positive_fold_ratio: float | None,
    sharpe: float | None,
    max_drawdown: float | None,
    n_trades: int | None,
    worst_fold_sharpe: float | None = None,
) -> CandidateGateResult:
    """Check the documented promotion thresholds against real candidate evidence.

    worst_fold_sharpe 是 mining-v3 起的稳健维度: strict 档 (confidence=high) 要求
    最差外层折 Sharpe > 0, 防"均值好看、某段崩溃"的组合登顶。None 表示 v2 历史
    工件无此列 — 历史候选按其产出时的口径放行, 不追溯执行新门槛。
    """
    reasons: list[str] = []
    if confidence == "low":
        reasons.append("exploratory results can only be saved as pending candidates")
    if valid_folds is None or valid_folds < GATE_MIN_VALID_FOLDS:
        reasons.append(
            "requires at least "
            f"{GATE_MIN_VALID_FOLDS} valid outer folds (got "
            f"{'none' if valid_folds is None else valid_folds})"
        )
    if positive_fold_ratio is None or positive_fold_ratio < GATE_MIN_POSITIVE_FOLD_RATIO:
        reasons.append(
            "requires a positive-return fold ratio of at least "
            f"{GATE_MIN_POSITIVE_FOLD_RATIO:.2f}"
        )
    if sharpe is None or sharpe < GATE_MIN_OOS_SHARPE:
        reasons.append(f"requires an OOS Sharpe of at least {GATE_MIN_OOS_SHARPE}")
    if (
        confidence == "high"
        and worst_fold_sharpe is not None
        and worst_fold_sharpe <= 0.0
    ):
        reasons.append(
            f"strict profile requires a positive worst-fold Sharpe (got {worst_fold_sharpe:.3f})"
        )
    if max_drawdown is None or max_drawdown < GATE_MAX_DRAWDOWN:
        reasons.append(
            f"requires a max drawdown no worse than {abs(GATE_MAX_DRAWDOWN):.0%}"
        )
    if n_trades is None or n_trades < GATE_MIN_TRADES:
        reasons.append(f"requires at least {GATE_MIN_TRADES} OOS trades")
    return CandidateGateResult(qualified=not reasons, reasons=tuple(reasons))


@dataclass(frozen=True)
class MiningRequest(JsonDataclassMixin):
    factor_names: tuple[str, ...]
    existing_strategy_ids: tuple[str, ...] = ()
    correlation_threshold: float = 0.8
    date_column: str = "date"
    target_column: str = "_next_return"
    budget: MiningBudget = field(default_factory=MiningBudget.balanced)
    validation: NestedValidationConfig = field(
        default_factory=NestedValidationConfig.balanced
    )
    profile: Literal["exploratory", "balanced", "strict"] = "balanced"

    def __post_init__(self) -> None:
        if not self.factor_names:
            raise ValueError("factor_names must not be empty")
        if len(set(self.factor_names)) != len(self.factor_names):
            raise ValueError("factor_names must not contain duplicates")
        if len(self.factor_names) > self.budget.max_factors:
            raise ValueError(
                f"factor count {len(self.factor_names)} exceeds budget "
                f"{self.budget.max_factors}"
            )
        if len(set(self.existing_strategy_ids)) != len(self.existing_strategy_ids):
            raise ValueError("existing_strategy_ids must not contain duplicates")
        if len(self.existing_strategy_ids) > self.budget.max_existing_strategies:
            raise ValueError(
                f"existing strategy count {len(self.existing_strategy_ids)} exceeds "
                f"budget {self.budget.max_existing_strategies}"
            )
        if not 0.0 < self.correlation_threshold <= 1.0:
            raise ValueError("correlation_threshold must be in (0, 1]")
        if not self.date_column or not self.target_column:
            raise ValueError("date_column and target_column must not be empty")

    @classmethod
    def for_profile(
        cls,
        profile: Literal["exploratory", "balanced", "strict"],
        factor_names: Sequence[str],
        existing_strategy_ids: Sequence[str] = (),
        **kwargs: Any,
    ) -> MiningRequest:
        if profile not in {"exploratory", "balanced", "strict"}:
            raise ValueError(f"unknown mining profile: {profile}")
        return cls(
            factor_names=tuple(factor_names),
            existing_strategy_ids=tuple(existing_strategy_ids),
            budget=getattr(MiningBudget, profile)(),
            validation=getattr(NestedValidationConfig, profile)(),
            profile=profile,
            **kwargs,
        )


@dataclass(frozen=True)
class FactorMetric(JsonDataclassMixin):
    factor_id: str
    composite_score: float
    ir: float
    coverage: float
    turnover: float
    rank_ic: float = 0.0


@dataclass(frozen=True)
class CorrelationResult(JsonDataclassMixin):
    factor_names: tuple[str, ...]
    matrix: tuple[tuple[float, ...], ...]
    pair_counts: tuple[tuple[int, ...], ...]
    elapsed_ms: float
    n_dates: int
    n_rows: int
    timing_ms: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class FactorExclusion(JsonDataclassMixin):
    factor_id: str
    reason: str
    representative: str
    rho: float


@dataclass(frozen=True)
class PruneResult(JsonDataclassMixin):
    selected: tuple[str, ...]
    excluded: tuple[FactorExclusion, ...]


@dataclass(frozen=True)
class MiningCandidate(JsonDataclassMixin):
    candidate_id: str
    kind: Literal["factor_rank", "existing_strategy"]
    factor_names: tuple[str, ...] = ()
    weights: tuple[float, ...] = ()
    directions: tuple[int, ...] = ()
    strategy_id: str | None = None
    proxy_rank_ic: float = 0.0
    proxy_ir: float = 0.0
    observations: int = 0
    dates: int = 0

    def definition(self) -> dict[str, Any]:
        if self.kind == "existing_strategy":
            return {"kind": self.kind, "strategy_id": self.strategy_id}
        return {
            "kind": self.kind,
            "factor_names": list(self.factor_names),
            "scoring": dict(zip(self.factor_names, self.weights, strict=True)),
            "directions": {
                factor_id: "high" if direction > 0 else "low"
                for factor_id, direction in zip(
                    self.factor_names,
                    self.directions,
                    strict=True,
                )
            },
        }


@dataclass(frozen=True)
class BeamSearchResult(JsonDataclassMixin):
    candidates: tuple[MiningCandidate, ...]
    trials_used: int
    cancelled: bool
    budget_exhausted: bool
    elapsed_ms: float


@dataclass(frozen=True)
class ValidationFold(JsonDataclassMixin):
    level: Literal["outer", "inner"]
    outer_index: int
    inner_index: int | None
    train_labels: tuple[str, ...]
    purge_labels: tuple[str, ...]
    test_labels: tuple[str, ...]
    embargo_labels: tuple[str, ...]

    @property
    def train_start(self) -> str:
        return self.train_labels[0]

    @property
    def train_end(self) -> str:
        return self.train_labels[-1]

    @property
    def test_start(self) -> str:
        return self.test_labels[0]

    @property
    def test_end(self) -> str:
        return self.test_labels[-1]


@dataclass(frozen=True)
class NestedFold(JsonDataclassMixin):
    outer: ValidationFold
    inner: tuple[ValidationFold, ...]


@dataclass(frozen=True)
class CandidateEvaluation(JsonDataclassMixin):
    score: float | None
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class FoldMiningResult(JsonDataclassMixin):
    outer_index: int
    selected_factors: tuple[str, ...]
    candidates: tuple[MiningCandidate, ...]
    selected_candidate_id: str | None
    inner_score: float | None
    outer_evaluation: CandidateEvaluation | None
    error: str | None = None
    benchmark_evaluations: tuple[tuple[str, CandidateEvaluation], ...] = ()
    cross_evaluations: tuple[tuple[str, CandidateEvaluation], ...] = ()


@dataclass(frozen=True)
class MiningResult(JsonDataclassMixin):
    request: MiningRequest
    folds: tuple[FoldMiningResult, ...]
    proxy_trials_used: int
    trials_used: int
    cancelled: bool
    elapsed_ms: float


class CandidateEvaluator(Protocol):
    def evaluate_candidate(
        self,
        train: pl.DataFrame,
        test: pl.DataFrame,
        definition: Mapping[str, Any],
    ) -> CandidateEvaluation | Mapping[str, Any] | float: ...


class FactorMetricProvider(Protocol):
    def __call__(
        self,
        train: pl.DataFrame,
        factor_names: Sequence[str],
    ) -> Sequence[FactorMetric]: ...


CancelCheck = Callable[[], bool] | Any


def compute_rank_correlation(
    panel: pl.DataFrame,
    factor_names: Sequence[str],
    start: Any | None = None,
    end: Any | None = None,
    *,
    date_column: str = "date",
) -> CorrelationResult:
    """Average pairwise daily cross-sectional rank correlations.

    One date partition is ranked and materialized at a time. Only the factor-by-factor
    correlation sums and valid-day counts remain resident across dates.
    """
    started = time.perf_counter()
    names = _validate_factor_names(panel, factor_names)
    if date_column not in panel.columns:
        raise ValueError(f"panel is missing date column {date_column!r}")

    filter_started = time.perf_counter()
    date_expr = pl.col(date_column).cast(pl.Utf8).str.slice(0, 10)
    scoped = panel.select([
        pl.col(date_column),
        *(
            pl.when(pl.col(name).is_finite())
            .then(pl.col(name))
            .otherwise(None)
            .alias(name)
            for name in names
        ),
    ])
    if start is not None:
        scoped = scoped.filter(date_expr >= str(start)[:10])
    if end is not None:
        scoped = scoped.filter(date_expr <= str(end)[:10])
    if scoped.is_empty():
        raise ValueError("rank correlation date range contains no panel rows")
    filter_ms = (time.perf_counter() - filter_started) * 1000.0

    width = len(names)
    pair_columns: dict[tuple[int, int], str] = {}
    pair_expressions: list[pl.Expr] = []
    for left in range(width):
        for right in range(left + 1, width):
            column = f"_rho_{left}_{right}"
            pair_columns[(left, right)] = column
            pair_expressions.append(
                pl.corr(names[left], names[right], method="spearman").alias(column)
            )
    observation_columns = [f"_n_{index}" for index in range(width)]

    rank_started = time.perf_counter()
    daily = scoped.group_by(date_column).agg([
        *pair_expressions,
        *(
            pl.col(name).count().alias(column)
            for name, column in zip(names, observation_columns, strict=True)
        ),
    ])
    rank_ms = (time.perf_counter() - rank_started) * 1000.0

    finish_started = time.perf_counter()
    correlation = np.full((width, width), np.nan, dtype=np.float64)
    counts = np.zeros((width, width), dtype=np.int32)
    for (left, right), column in pair_columns.items():
        values = daily.get_column(column).to_numpy()
        finite = np.isfinite(values)
        count = int(np.count_nonzero(finite))
        if count:
            value = float(np.mean(values[finite]))
            correlation[left, right] = correlation[right, left] = value
            counts[left, right] = counts[right, left] = count
    for index, column in enumerate(observation_columns):
        count = int(np.count_nonzero(daily.get_column(column).to_numpy() >= 2))
        if count:
            correlation[index, index] = 1.0
            counts[index, index] = count
    finish_ms = (time.perf_counter() - finish_started) * 1000.0
    n_dates = daily.height
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    timing = {
        "filter": round(filter_ms, 3),
        "rank_accumulate": round(rank_ms, 3),
        "finalize": round(finish_ms, 3),
        "total": round(elapsed_ms, 3),
    }
    logger.info(
        "mining rank correlation factors=%d rows=%d dates=%d elapsed_ms=%.1f",
        width,
        scoped.height,
        n_dates,
        elapsed_ms,
    )
    return CorrelationResult(
        factor_names=names,
        matrix=tuple(tuple(float(value) for value in row) for row in correlation),
        pair_counts=tuple(
            tuple(int(value) for value in row)
            for row in counts
        ),
        elapsed_ms=round(elapsed_ms, 3),
        n_dates=n_dates,
        n_rows=scoped.height,
        timing_ms=timing,
    )


def prune_correlated_factors(
    metrics: Sequence[FactorMetric],
    correlation: CorrelationResult,
    threshold: float,
) -> PruneResult:
    """Keep the strongest deterministic representative of correlated factors."""
    if not 0.0 < threshold <= 1.0:
        raise ValueError("correlation threshold must be in (0, 1]")
    by_name = {name: index for index, name in enumerate(correlation.factor_names)}
    if len({metric.factor_id for metric in metrics}) != len(metrics):
        raise ValueError("factor metrics must not contain duplicate factor_id values")
    missing = sorted(metric.factor_id for metric in metrics if metric.factor_id not in by_name)
    if missing:
        raise ValueError(f"factor metrics missing from correlation result: {missing}")

    ordered = sorted(
        metrics,
        key=lambda metric: (
            -_finite_sort_value(metric.composite_score),
            -_finite_sort_value(metric.ir),
            -_finite_sort_value(metric.coverage),
            _finite_sort_value(metric.turnover, worst=float("inf")),
            metric.factor_id,
        ),
    )
    selected: list[str] = []
    excluded: list[FactorExclusion] = []
    for metric in ordered:
        factor_index = by_name[metric.factor_id]
        correlated = [
            (representative, correlation.matrix[factor_index][by_name[representative]])
            for representative in selected
            if (
                correlation.pair_counts[factor_index][by_name[representative]] > 0
                and math.isfinite(
                    correlation.matrix[factor_index][by_name[representative]]
                )
                and abs(
                    correlation.matrix[factor_index][by_name[representative]]
                ) >= threshold
            )
        ]
        if not correlated:
            selected.append(metric.factor_id)
            continue
        representative, rho = max(
            correlated,
            key=lambda item: (abs(item[1]), -selected.index(item[0])),
        )
        excluded.append(FactorExclusion(
            factor_id=metric.factor_id,
            reason="correlation_threshold",
            representative=representative,
            rho=round(float(rho), 8),
        ))
    return PruneResult(selected=tuple(selected), excluded=tuple(excluded))


def beam_search_factor_combinations(
    panel: pl.DataFrame,
    factor_names: Sequence[str],
    *,
    target_column: str = "_next_return",
    date_column: str = "date",
    train_start: Any | None = None,
    train_end: Any | None = None,
    max_combination_size: int = MAX_COMBINATION_SIZE,
    beam_width: int = 16,
    max_trials: int = 256,
    cancel_check: CancelCheck | None = None,
) -> BeamSearchResult:
    """Search equal and one-factor-double rank combinations on training data only."""
    started = time.perf_counter()
    names = _validate_factor_names(panel, factor_names)
    if target_column not in panel.columns:
        raise ValueError(f"panel is missing target column {target_column!r}")
    if date_column not in panel.columns:
        raise ValueError(f"panel is missing date column {date_column!r}")
    if not 1 <= max_combination_size <= MAX_COMBINATION_SIZE:
        raise ValueError(
            f"max_combination_size must be between 1 and {MAX_COMBINATION_SIZE}"
        )
    if not 1 <= beam_width <= MAX_BEAM_WIDTH:
        raise ValueError(f"beam_width must be between 1 and {MAX_BEAM_WIDTH}")
    if max_trials <= 0:
        raise ValueError("max_trials must be positive")

    date_expr = pl.col(date_column).cast(pl.Utf8).str.slice(0, 10)
    scoped = panel.select([date_column, *names, target_column])
    if train_start is not None:
        scoped = scoped.filter(date_expr >= str(train_start)[:10])
    if train_end is not None:
        scoped = scoped.filter(date_expr <= str(train_end)[:10])
    if scoped.is_empty():
        raise ValueError("beam search training range contains no panel rows")

    ordered_names = tuple(sorted(names))
    name_to_column = {name: index for index, name in enumerate(ordered_names)}
    if not scoped.get_column(date_column).is_sorted():
        scoped = scoped.sort(date_column)
    blocks = tuple(
        daily.select([*ordered_names, target_column]).to_numpy()
        for daily in scoped.partition_by(date_column, maintain_order=True)
    )
    trials_used = 0
    cancelled = False

    def evaluate(
        factors: tuple[str, ...],
        weights: tuple[float, ...],
        directions: tuple[int, ...],
    ) -> tuple[float, float, int, int] | None:
        nonlocal trials_used, cancelled
        if trials_used >= max_trials:
            return None
        if _cancelled(cancel_check):
            cancelled = True
            return None
        trials_used += 1
        columns = tuple(name_to_column[name] for name in factors)
        return _proxy_rank_ic(blocks, columns, weights, directions)

    directions_by_factor: dict[str, int] = {}
    single_candidates: list[MiningCandidate] = []
    for factor_name in ordered_names:
        proxy = evaluate((factor_name,), (1.0,), (1,))
        if proxy is None:
            break
        rank_ic, proxy_ir, observations, n_dates = proxy
        if n_dates <= 0 or observations < 3:
            continue
        direction = 1 if rank_ic >= 0.0 else -1
        directions_by_factor[factor_name] = direction
        single_candidates.append(_factor_candidate(
            (factor_name,),
            (1.0,),
            (direction,),
            abs(rank_ic),
            abs(proxy_ir),
            observations,
            n_dates,
        ))

    current_beam = _rank_candidates(single_candidates)[:beam_width]
    retained = list(current_beam)
    searchable_names = tuple(sorted(directions_by_factor))
    max_size = min(max_combination_size, len(searchable_names))
    for size in range(2, max_size + 1):
        if cancelled or trials_used >= max_trials or not current_beam:
            break
        factor_sets: set[tuple[str, ...]] = set()
        for candidate in current_beam:
            for factor_name in searchable_names:
                combined = tuple(sorted({*candidate.factor_names, factor_name}))
                if len(combined) == size:
                    factor_sets.add(combined)

        expanded: list[MiningCandidate] = []
        stop = False
        for factors in sorted(factor_sets):
            directions = tuple(directions_by_factor[name] for name in factors)
            patterns = [(1.0,) * size]
            patterns.extend(
                tuple(2.0 if index == doubled else 1.0 for index in range(size))
                for doubled in range(size)
            )
            for weights in patterns:
                proxy = evaluate(factors, weights, directions)
                if proxy is None:
                    stop = True
                    break
                rank_ic, proxy_ir, observations, n_dates = proxy
                if n_dates <= 0 or observations < 3:
                    continue
                expanded.append(_factor_candidate(
                    factors,
                    weights,
                    directions,
                    rank_ic,
                    proxy_ir,
                    observations,
                    n_dates,
                ))
            if stop:
                break
        current_beam = _rank_candidates(expanded)[:beam_width]
        retained.extend(current_beam)
        if stop:
            break

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return BeamSearchResult(
        candidates=tuple(_rank_candidates(retained)),
        trials_used=trials_used,
        cancelled=cancelled,
        budget_exhausted=trials_used >= max_trials,
        elapsed_ms=round(elapsed_ms, 3),
    )


def generate_nested_folds(
    trading_labels: Sequence[Any],
    config: NestedValidationConfig,
) -> tuple[NestedFold, ...]:
    """Generate rolling nested folds from ordered trading labels, not calendar days."""
    labels = tuple(dict.fromkeys(str(label) for label in trading_labels))
    if not labels:
        raise ValueError("trading labels are empty")
    if tuple(sorted(labels)) != labels:
        raise ValueError("trading labels must be sorted in ascending order")
    outer_required = (
        config.outer_train_bars + config.purge_bars + config.outer_test_bars
    )
    if len(labels) < outer_required:
        raise ValueError(
            "insufficient trading bars for outer validation: "
            f"need at least {outer_required}, got {len(labels)}"
        )
    inner_required = (
        config.inner_train_bars + config.purge_bars + config.inner_test_bars
    )
    if config.outer_train_bars < inner_required:
        raise ValueError(
            "outer training window is too short for one inner fold: "
            f"need at least {inner_required}, got {config.outer_train_bars}"
        )

    nested: list[NestedFold] = []
    outer_start = 0
    outer_index = 0
    while outer_start + outer_required <= len(labels):
        outer = _make_validation_fold(
            labels,
            level="outer",
            outer_index=outer_index,
            inner_index=None,
            train_start=outer_start,
            train_bars=config.outer_train_bars,
            test_bars=config.outer_test_bars,
            purge_bars=config.purge_bars,
            embargo_bars=config.embargo_bars,
        )
        inner_folds: list[ValidationFold] = []
        inner_start = outer_start
        inner_index = 0
        outer_train_stop = outer_start + config.outer_train_bars
        while inner_start + inner_required <= outer_train_stop:
            inner_folds.append(_make_validation_fold(
                labels,
                level="inner",
                outer_index=outer_index,
                inner_index=inner_index,
                train_start=inner_start,
                train_bars=config.inner_train_bars,
                test_bars=config.inner_test_bars,
                purge_bars=config.purge_bars,
                embargo_bars=config.embargo_bars,
                hard_stop=outer_train_stop,
            ))
            inner_index += 1
            inner_start += config.inner_step_bars
        if not inner_folds:
            raise ValueError(f"outer fold {outer_index} contains no valid inner fold")
        nested.append(NestedFold(outer=outer, inner=tuple(inner_folds)))
        outer_index += 1
        outer_start += config.outer_step_bars
    return tuple(nested)


class MiningService:
    """Run leakage-bounded mining and delegate real backtests to a callback."""

    def run(
        self,
        panel: pl.DataFrame,
        request: MiningRequest,
        *,
        factor_metrics: Sequence[FactorMetric] | None = None,
        metric_provider: FactorMetricProvider | None = None,
        evaluator: CandidateEvaluator | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> MiningResult:
        started = time.perf_counter()
        _validate_factor_names(panel, request.factor_names)
        required = {request.date_column, request.target_column}
        missing = sorted(required - set(panel.columns))
        if missing:
            raise ValueError(f"mining panel is missing required columns: {missing}")
        if evaluator is not None:
            if factor_metrics is not None:
                raise ValueError("factor_metrics must not be supplied with evaluator")
            if metric_provider is None:
                raise ValueError("metric_provider is required when evaluator is supplied")
        elif metric_provider is None and factor_metrics is None:
            raise ValueError("factor_metrics or metric_provider is required")

        labels = panel.select(
            pl.col(request.date_column).cast(pl.Utf8).str.slice(0, 10).unique().sort()
        ).to_series().to_list()
        nested_folds = generate_nested_folds(labels, request.validation)
        if request.profile != "exploratory" and len(nested_folds) < 3:
            raise ValueError(
                f"{request.profile} mining requires at least 3 outer folds; "
                f"got {len(nested_folds)}"
            )
        fold_results: list[FoldMiningResult] = []
        proxy_trials_used = 0
        trials_used = 0
        cancelled = False
        labels_evaluator = (
            getattr(evaluator, "evaluate_candidate_labels", None)
            if evaluator is not None
            else None
        )

        def run_benchmarks(nested_fold) -> tuple[tuple[str, CandidateEvaluation], ...]:
            """Score every user-selected strategy on one outer test window.

            Benchmarks document how existing strategies would have done on the
            same walk-forward windows, so they are evaluated even when the
            factor track fails on this fold and never join the winner race.
            """
            nonlocal trials_used, cancelled
            if evaluator is None:
                return ()
            benchmarks: list[tuple[str, CandidateEvaluation]] = []
            for strategy_id in request.existing_strategy_ids:
                if _cancelled(cancel_check):
                    cancelled = True
                    break
                if trials_used >= request.budget.max_trials:
                    benchmarks.append((
                        _benchmark_signature(strategy_id),
                        CandidateEvaluation(
                            score=None,
                            error="real trial budget exhausted before benchmark evaluation",
                        ),
                    ))
                    continue
                benchmark = benchmark_candidate(strategy_id)
                if callable(labels_evaluator):
                    benchmark_evaluation = _evaluate_labels(
                        labels_evaluator,
                        nested_fold.outer.train_labels,
                        nested_fold.outer.test_labels,
                        benchmark,
                    )
                else:
                    benchmark_evaluation = _evaluate(
                        evaluator,
                        _train_panel_for_fold(panel, request.date_column, nested_fold.outer),
                        _panel_for_labels(panel, request.date_column, nested_fold.outer.test_labels),
                        benchmark,
                    )
                trials_used += 1
                benchmarks.append((benchmark.candidate_id, benchmark_evaluation))
            return tuple(benchmarks)

        if evaluator is None:
            selection_phases_remaining = len(nested_folds)
            evaluation_phases_remaining = 0
        else:
            selection_phases_remaining = sum(
                len(nested.inner) + 1 for nested in nested_folds
            )
            evaluation_phases_remaining = selection_phases_remaining

        for nested in nested_folds:
            if _cancelled(cancel_check):
                cancelled = True
                break

            if evaluator is None:
                outer_train = _train_panel_for_fold(
                    panel,
                    request.date_column,
                    nested.outer,
                )
                metrics = tuple(
                    metric_provider(outer_train, request.factor_names)
                    if metric_provider is not None
                    else factor_metrics or ()
                )
                correlation = compute_rank_correlation(
                    outer_train,
                    request.factor_names,
                    date_column=request.date_column,
                )
                pruned = prune_correlated_factors(
                    metrics,
                    correlation,
                    request.correlation_threshold,
                )
                proxy_remaining = request.budget.max_proxy_trials - proxy_trials_used
                if proxy_remaining <= 0:
                    fold_results.append(FoldMiningResult(
                        outer_index=nested.outer.outer_index,
                        selected_factors=pruned.selected,
                        candidates=(),
                        selected_candidate_id=None,
                        inner_score=None,
                        outer_evaluation=None,
                        error="proxy trial budget exhausted",
                    ))
                    break
                proxy_allowance = max(
                    1,
                    proxy_remaining // selection_phases_remaining,
                )
                beam = beam_search_factor_combinations(
                    outer_train,
                    _searchable_factors(pruned.selected, request.budget),
                    target_column=request.target_column,
                    date_column=request.date_column,
                    max_combination_size=request.budget.max_combination_size,
                    beam_width=request.budget.beam_width,
                    max_trials=proxy_allowance,
                    cancel_check=cancel_check,
                )
                proxy_trials_used += beam.trials_used
                selection_phases_remaining -= 1
                candidates = beam.candidates
                fold_results.append(FoldMiningResult(
                    outer_index=nested.outer.outer_index,
                    selected_factors=pruned.selected,
                    candidates=tuple(candidates),
                    selected_candidate_id=None,
                    inner_score=None,
                    outer_evaluation=None,
                ))
                continue

            winners: list[tuple[float, MiningCandidate]] = []
            all_candidates: dict[str, MiningCandidate] = {}
            selected_factor_union: set[str] = set()
            for inner in nested.inner:
                if _cancelled(cancel_check):
                    cancelled = True
                    break
                inner_train = _train_panel_for_fold(
                    panel,
                    request.date_column,
                    inner,
                )
                inner_test = _panel_for_labels(
                    panel,
                    request.date_column,
                    inner.test_labels,
                )
                metrics = tuple(
                    metric_provider(inner_train, request.factor_names)
                    if metric_provider is not None
                    else factor_metrics or ()
                )
                correlation = compute_rank_correlation(
                    inner_train,
                    request.factor_names,
                    date_column=request.date_column,
                )
                pruned = prune_correlated_factors(
                    metrics,
                    correlation,
                    request.correlation_threshold,
                )
                selected_factor_union.update(pruned.selected)

                proxy_remaining = request.budget.max_proxy_trials - proxy_trials_used
                if proxy_remaining <= 0:
                    break
                proxy_allowance = max(
                    1,
                    proxy_remaining // selection_phases_remaining,
                )
                beam = beam_search_factor_combinations(
                    inner_train,
                    _searchable_factors(pruned.selected, request.budget),
                    target_column=request.target_column,
                    date_column=request.date_column,
                    max_combination_size=request.budget.max_combination_size,
                    beam_width=request.budget.beam_width,
                    max_trials=proxy_allowance,
                    cancel_check=cancel_check,
                )
                proxy_trials_used += beam.trials_used
                selection_phases_remaining -= 1
                candidates = beam.candidates
                for candidate in candidates:
                    all_candidates[candidate.candidate_id] = candidate

                real_remaining = request.budget.max_trials - trials_used
                if real_remaining <= 0:
                    break
                real_allowance = min(
                    MAX_FINALISTS,
                    max(1, real_remaining // evaluation_phases_remaining),
                )
                evaluated: list[tuple[float, MiningCandidate]] = []
                for candidate in candidates[:real_allowance]:
                    evaluation = (
                        _evaluate_labels(
                            labels_evaluator,
                            inner.train_labels,
                            inner.test_labels,
                            candidate,
                        )
                        if callable(labels_evaluator)
                        else _evaluate(
                            evaluator,
                            inner_train,
                            inner_test,
                            candidate,
                        )
                    )
                    trials_used += 1
                    if evaluation.error is None and evaluation.score is not None:
                        evaluated.append((evaluation.score, candidate))
                evaluation_phases_remaining -= 1
                if evaluated:
                    winners.append(min(
                        evaluated,
                        key=lambda item: (-item[0], item[1].candidate_id),
                    ))

            if cancelled:
                break
            if not winners:
                fold_results.append(FoldMiningResult(
                    outer_index=nested.outer.outer_index,
                    selected_factors=tuple(sorted(selected_factor_union)),
                    candidates=tuple(_rank_candidates(list(all_candidates.values()))),
                    selected_candidate_id=None,
                    inner_score=None,
                    outer_evaluation=None,
                    error="no candidate completed inner validation within budget",
                    benchmark_evaluations=run_benchmarks(nested),
                ))
                break

            by_candidate: dict[str, list[float]] = {}
            for score, candidate in winners:
                by_candidate.setdefault(candidate.candidate_id, []).append(score)
            selected_id, selected_scores = min(
                by_candidate.items(),
                key=lambda item: (-len(item[1]), -float(np.mean(item[1])), item[0]),
            )
            voted_candidate = all_candidates[selected_id]
            inner_score = float(np.mean(selected_scores))
            if not callable(labels_evaluator):
                del inner_train, inner_test

            outer_train = _train_panel_for_fold(
                panel,
                request.date_column,
                nested.outer,
            )
            outer_metrics = tuple(
                metric_provider(outer_train, request.factor_names)
                if metric_provider is not None
                else factor_metrics or ()
            )
            outer_correlation = compute_rank_correlation(
                outer_train,
                request.factor_names,
                date_column=request.date_column,
            )
            outer_pruned = prune_correlated_factors(
                outer_metrics,
                outer_correlation,
                request.correlation_threshold,
            )
            proxy_remaining = request.budget.max_proxy_trials - proxy_trials_used
            if proxy_remaining <= 0:
                fold_results.append(FoldMiningResult(
                    outer_index=nested.outer.outer_index,
                    selected_factors=outer_pruned.selected,
                    candidates=tuple(_rank_candidates(list(all_candidates.values()))),
                    selected_candidate_id=None,
                    inner_score=round(inner_score, 8),
                    outer_evaluation=None,
                    error="proxy trial budget exhausted before outer retraining",
                    benchmark_evaluations=run_benchmarks(nested),
                ))
                break
            proxy_allowance = max(
                1,
                proxy_remaining // selection_phases_remaining,
            )
            outer_beam = beam_search_factor_combinations(
                outer_train,
                _searchable_factors(outer_pruned.selected, request.budget),
                target_column=request.target_column,
                date_column=request.date_column,
                max_combination_size=request.budget.max_combination_size,
                beam_width=request.budget.beam_width,
                max_trials=proxy_allowance,
                cancel_check=cancel_check,
            )
            proxy_trials_used += outer_beam.trials_used
            selection_phases_remaining -= 1
            outer_candidates = outer_beam.candidates
            by_refit_key = {
                _candidate_refit_key(candidate): candidate
                for candidate in outer_candidates
            }
            selected = by_refit_key.get(_candidate_refit_key(voted_candidate))
            if selected is None:
                fold_results.append(FoldMiningResult(
                    outer_index=nested.outer.outer_index,
                    selected_factors=outer_pruned.selected,
                    candidates=outer_candidates,
                    selected_candidate_id=None,
                    inner_score=round(inner_score, 8),
                    outer_evaluation=None,
                    error=(
                        "outer retraining did not reproduce selected candidate structure"
                        if outer_candidates
                        else "outer training produced no candidate"
                    ),
                    benchmark_evaluations=run_benchmarks(nested),
                ))
                break

            if trials_used >= request.budget.max_trials:
                fold_results.append(FoldMiningResult(
                    outer_index=nested.outer.outer_index,
                    selected_factors=outer_pruned.selected,
                    candidates=outer_candidates,
                    selected_candidate_id=selected.candidate_id,
                    inner_score=round(inner_score, 8),
                    outer_evaluation=None,
                    error="real trial budget exhausted before outer evaluation",
                    benchmark_evaluations=run_benchmarks(nested),
                ))
                break
            outer_test = _panel_for_labels(
                panel,
                request.date_column,
                nested.outer.test_labels,
            )
            if callable(labels_evaluator):
                del outer_train, outer_test
                outer_evaluation = _evaluate_labels(
                    labels_evaluator,
                    nested.outer.train_labels,
                    nested.outer.test_labels,
                    selected,
                )
            else:
                outer_evaluation = _evaluate(
                    evaluator,
                    outer_train,
                    outer_test,
                    selected,
                )
            trials_used += 1
            evaluation_phases_remaining -= 1
            fold_results.append(FoldMiningResult(
                outer_index=nested.outer.outer_index,
                selected_factors=outer_pruned.selected,
                candidates=outer_candidates,
                selected_candidate_id=selected.candidate_id,
                inner_score=round(inner_score, 8),
                outer_evaluation=outer_evaluation,
                benchmark_evaluations=run_benchmarks(nested),
            ))
            if cancelled:
                break
            if trials_used >= request.budget.max_trials:
                break

        if not cancelled and evaluator is not None:
            winner_union: dict[str, MiningCandidate] = {}
            for fold in fold_results:
                if fold.selected_candidate_id is None:
                    continue
                if fold.selected_candidate_id in winner_union:
                    continue
                winner = next(
                    (
                        candidate
                        for candidate in fold.candidates
                        if candidate.candidate_id == fold.selected_candidate_id
                    ),
                    None,
                )
                if winner is not None:
                    winner_union[winner.candidate_id] = winner
            if len(winner_union) > 1:
                extended_folds: list[FoldMiningResult] = []
                for nested, fold in zip(nested_folds, fold_results, strict=False):
                    cross: list[tuple[str, CandidateEvaluation]] = []
                    for candidate_id, candidate in winner_union.items():
                        if candidate_id == fold.selected_candidate_id:
                            continue
                        if trials_used >= request.budget.max_trials:
                            cross.append((
                                candidate_id,
                                CandidateEvaluation(
                                    score=None,
                                    error=(
                                        "real trial budget exhausted before "
                                        "cross-fold evaluation"
                                    ),
                                ),
                            ))
                            continue
                        if callable(labels_evaluator):
                            evaluation = _evaluate_labels(
                                labels_evaluator,
                                nested.outer.train_labels,
                                nested.outer.test_labels,
                                candidate,
                            )
                        else:
                            cross_train = _train_panel_for_fold(
                                panel,
                                request.date_column,
                                nested.outer,
                            )
                            cross_test = _panel_for_labels(
                                panel,
                                request.date_column,
                                nested.outer.test_labels,
                            )
                            evaluation = _evaluate(
                                evaluator,
                                cross_train,
                                cross_test,
                                candidate,
                            )
                        trials_used += 1
                        cross.append((candidate_id, evaluation))
                    extended_folds.append(replace(
                        fold,
                        cross_evaluations=tuple(cross),
                    ))
                fold_results = extended_folds

        return MiningResult(
            request=request,
            folds=tuple(fold_results),
            proxy_trials_used=proxy_trials_used,
            trials_used=trials_used,
            cancelled=cancelled,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )


def _validate_factor_names(
    panel: pl.DataFrame,
    factor_names: Sequence[str],
) -> tuple[str, ...]:
    names = tuple(str(name) for name in factor_names)
    if not names:
        raise ValueError("factor_names must not be empty")
    if len(names) > MAX_MINING_FACTORS:
        raise ValueError(f"factor count exceeds hard limit {MAX_MINING_FACTORS}")
    if len(set(names)) != len(names):
        raise ValueError("factor_names must not contain duplicates")
    missing = sorted(set(names) - set(panel.columns))
    if missing:
        raise ValueError(f"panel is missing factor columns: {missing}")
    return names


def _finite_sort_value(value: float, *, worst: float = float("-inf")) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return worst
    return number if math.isfinite(number) else worst


def _average_rank(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="stable")
    ordered = values[order]
    boundaries = np.concatenate((
        np.array([0]),
        np.flatnonzero(ordered[1:] != ordered[:-1]) + 1,
        np.array([len(ordered)]),
    ))
    counts = np.diff(boundaries)
    average = (boundaries[:-1] + 1 + boundaries[1:]) / 2.0
    ranked = np.empty(len(values), dtype=np.float64)
    ranked[order] = np.repeat(average, counts)
    return ranked


def _proxy_rank_ic(
    blocks: Sequence[np.ndarray],
    columns: tuple[int, ...],
    weights: tuple[float, ...],
    directions: tuple[int, ...],
) -> tuple[float, float, int, int]:
    daily_ics: list[float] = []
    observations = 0
    weight_values = np.asarray(weights, dtype=np.float64)
    direction_values = np.asarray(directions, dtype=np.float64)
    for block in blocks:
        factors = block[:, columns].astype(np.float64, copy=False)
        target = block[:, -1].astype(np.float64, copy=False)
        valid = np.isfinite(target) & np.isfinite(factors).all(axis=1)
        count = int(np.count_nonzero(valid))
        if count < 3:
            continue
        factor_values = factors[valid]
        factor_ranks = np.column_stack([
            _average_rank(factor_values[:, index])
            for index in range(factor_values.shape[1])
        ])
        composite = (factor_ranks * direction_values) @ weight_values
        composite_rank = _average_rank(composite)
        target_rank = _average_rank(target[valid])
        if np.std(composite_rank) <= 0.0 or np.std(target_rank) <= 0.0:
            continue
        rho = float(np.corrcoef(composite_rank, target_rank)[0, 1])
        if math.isfinite(rho):
            daily_ics.append(rho)
            observations += count
    if not daily_ics:
        return 0.0, 0.0, observations, 0
    values = np.asarray(daily_ics, dtype=np.float64)
    mean = float(np.mean(values))
    std = float(np.std(values))
    ir = mean / std if std > 1e-12 else 0.0
    return mean, ir, observations, len(daily_ics)


def compute_candidate_signature(definition: Mapping[str, Any]) -> str:
    """Return the canonical artifact signature for a mining candidate definition."""
    kind = definition.get("kind")
    if kind == "existing_strategy":
        strategy_id = definition.get("strategy_id")
        if not isinstance(strategy_id, str) or not strategy_id:
            raise ValueError("existing strategy candidate requires strategy_id")
        return f"strategy:{strategy_id}"
    if kind != "factor_rank":
        raise ValueError(f"unsupported mining candidate kind: {kind!r}")

    factor_names = definition.get("factor_names")
    scoring = definition.get("scoring")
    directions = definition.get("directions")
    if not isinstance(factor_names, list) or not factor_names:
        raise ValueError("factor candidate requires factor_names")
    if not isinstance(scoring, Mapping) or set(scoring) != set(factor_names):
        raise ValueError("factor candidate scoring keys must match factor_names")
    if not isinstance(directions, Mapping) or set(directions) != set(factor_names):
        raise ValueError("factor candidate direction keys must match factor_names")

    weights: list[str] = []
    direction_values: list[str] = []
    for factor_name in factor_names:
        if not isinstance(factor_name, str) or not factor_name:
            raise ValueError("factor names must be non-empty strings")
        value = scoring[factor_name]
        if isinstance(value, bool):
            raise ValueError("factor weights must be numeric")
        try:
            weight = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("factor weights must be numeric") from exc
        if not math.isfinite(weight) or weight <= 0.0:
            raise ValueError("factor weights must be finite and positive")
        direction = directions[factor_name]
        if direction not in {"high", "low"}:
            raise ValueError("factor directions must be high or low")
        weights.append(f"{weight:g}")
        direction_values.append("1" if direction == "high" else "-1")
    return (
        f"factor:{','.join(factor_names)}|w:{','.join(weights)}"
        f"|d:{','.join(direction_values)}"
    )


def _factor_candidate(
    factors: tuple[str, ...],
    weights: tuple[float, ...],
    directions: tuple[int, ...],
    rank_ic: float,
    proxy_ir: float,
    observations: int,
    dates: int,
) -> MiningCandidate:
    candidate_id = compute_candidate_signature({
        "kind": "factor_rank",
        "factor_names": list(factors),
        "scoring": dict(zip(factors, weights, strict=True)),
        "directions": {
            factor_id: "high" if direction > 0 else "low"
            for factor_id, direction in zip(factors, directions, strict=True)
        },
    })
    return MiningCandidate(
        candidate_id=candidate_id,
        kind="factor_rank",
        factor_names=factors,
        weights=weights,
        directions=directions,
        proxy_rank_ic=round(float(rank_ic), 8),
        proxy_ir=round(float(proxy_ir), 8),
        observations=observations,
        dates=dates,
    )


def benchmark_candidate(strategy_id: str) -> MiningCandidate:
    """Build the fixed benchmark candidate for one user-selected strategy."""
    return MiningCandidate(
        candidate_id=_benchmark_signature(strategy_id),
        kind="existing_strategy",
        strategy_id=strategy_id,
    )


def _benchmark_signature(strategy_id: str) -> str:
    return compute_candidate_signature({
        "kind": "existing_strategy",
        "strategy_id": strategy_id,
    })


def _searchable_factors(
    selected: Sequence[str],
    budget: MiningBudget,
) -> tuple[str, ...]:
    """Cap beam-search inputs so singletons plus combinations fit the phase budget.

    ``selected`` arrives in training-fold metric order, so the cap keeps the
    strongest factors and keeps the lexicographic singleton pass from consuming
    the whole proxy allowance before any combination is scored.
    """
    return tuple(selected[: max(1, budget.beam_width)])


def _candidate_refit_key(candidate: MiningCandidate) -> tuple[Any, ...]:
    if candidate.kind == "existing_strategy":
        return (candidate.kind, candidate.strategy_id)
    return (candidate.kind, candidate.factor_names, candidate.weights)


def _rank_candidates(candidates: Sequence[MiningCandidate]) -> list[MiningCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: (
            -candidate.proxy_rank_ic,
            -candidate.proxy_ir,
            candidate.candidate_id,
        ),
    )


def _make_validation_fold(
    labels: tuple[str, ...],
    *,
    level: Literal["outer", "inner"],
    outer_index: int,
    inner_index: int | None,
    train_start: int,
    train_bars: int,
    test_bars: int,
    purge_bars: int,
    embargo_bars: int,
    hard_stop: int | None = None,
) -> ValidationFold:
    train_stop = train_start + train_bars
    test_start = train_stop + purge_bars
    test_stop = test_start + test_bars
    embargo_stop = test_stop + embargo_bars
    if hard_stop is not None:
        embargo_stop = min(embargo_stop, hard_stop)
    return ValidationFold(
        level=level,
        outer_index=outer_index,
        inner_index=inner_index,
        train_labels=labels[train_start:train_stop],
        purge_labels=labels[train_stop:test_start],
        test_labels=labels[test_start:test_stop],
        embargo_labels=labels[test_stop:embargo_stop],
    )


def _train_panel_for_fold(
    panel: pl.DataFrame,
    date_column: str,
    fold: ValidationFold,
) -> pl.DataFrame:
    train = _panel_for_labels(panel, date_column, fold.train_labels)
    if "_target_date" not in train.columns:
        return train
    target_date = pl.col("_target_date").cast(pl.Utf8).str.slice(0, 10)
    daily = train.select(date_column, "_target_date").unique(
        subset=[date_column],
        maintain_order=True,
    )
    allowed = daily.select(
        (target_date.is_null() | (target_date <= fold.train_end)).alias("_allowed")
    ).get_column("_allowed").to_list()
    first_excluded = next(
        (index for index, value in enumerate(allowed) if not value),
        len(allowed),
    )
    if not any(allowed[first_excluded:]):
        return _panel_for_labels(
            train,
            date_column,
            fold.train_labels[:first_excluded],
        )
    return train.filter(target_date.is_null() | (target_date <= fold.train_end))


def _panel_for_labels(
    panel: pl.DataFrame,
    date_column: str,
    labels: Sequence[str],
) -> pl.DataFrame:
    normalized = tuple(str(label)[:10] for label in labels)
    if not normalized:
        return panel.slice(0, 0)
    values = panel.get_column(date_column)
    if values.is_sorted():
        bounds: tuple[Any, Any] | None = None
        if values.dtype == pl.Date:
            bounds = (
                date.fromisoformat(normalized[0]),
                date.fromisoformat(normalized[-1]),
            )
        elif values.dtype == pl.Utf8:
            bounds = (normalized[0], normalized[-1])
        if bounds is not None:
            start = int(values.search_sorted(bounds[0], side="left"))
            stop = int(values.search_sorted(bounds[1], side="right"))
            sliced = panel.slice(start, stop - start)
            actual = tuple(
                str(value)[:10]
                for value in sliced.get_column(date_column).unique(
                    maintain_order=True
                )
            )
            if actual == normalized:
                return sliced
    date_expr = pl.col(date_column).cast(pl.Utf8).str.slice(0, 10)
    return panel.filter(date_expr.is_in(normalized))


def _cancelled(cancel_check: CancelCheck | None) -> bool:
    if cancel_check is None:
        return False
    if callable(cancel_check):
        return bool(cancel_check())
    is_set = getattr(cancel_check, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _evaluate_labels(
    evaluator: Callable[
        [Sequence[str], Sequence[str], Mapping[str, Any]],
        CandidateEvaluation | Mapping[str, Any] | float,
    ],
    train_labels: Sequence[str],
    test_labels: Sequence[str],
    candidate: MiningCandidate,
) -> CandidateEvaluation:
    try:
        raw = evaluator(train_labels, test_labels, candidate.definition())
    except Exception as exc:
        return CandidateEvaluation(score=None, error=str(exc))
    return _normalize_evaluation(raw)


def _evaluate(
    evaluator: CandidateEvaluator,
    train: pl.DataFrame,
    test: pl.DataFrame,
    candidate: MiningCandidate,
) -> CandidateEvaluation:
    try:
        raw = evaluator.evaluate_candidate(train, test, candidate.definition())
    except Exception as exc:
        return CandidateEvaluation(score=None, error=str(exc))
    return _normalize_evaluation(raw)


def _normalize_evaluation(
    raw: CandidateEvaluation | Mapping[str, Any] | float,
) -> CandidateEvaluation:
    if isinstance(raw, CandidateEvaluation):
        return CandidateEvaluation(
            score=_finite_score(raw.score),
            metrics=raw.metrics,
            error=raw.error,
        )
    if isinstance(raw, Mapping):
        score = raw.get("score")
        error = raw.get("error")
        metrics = raw.get("metrics", {})
        return CandidateEvaluation(
            score=_finite_score(score),
            metrics=dict(metrics) if isinstance(metrics, Mapping) else {},
            error=str(error) if error is not None else None,
        )
    return CandidateEvaluation(score=_finite_score(raw))


def _finite_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) else None
