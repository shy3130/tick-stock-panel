"""运行 AlphaGPT 闭环 v1：进化搜索 vs 同预算随机搜索。

用法：
    cd backend
    .venv/Scripts/python.exe -m research.alphagpt.run_alphagpt_v1

搜索奖励只读取 T1-T3；HOLDOUT 在两路搜索结束、排名冻结后才评估最终候选。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import numpy as np
import polars as pl

from research.alphagpt.environment import AlphaEnvConfig
from research.alphagpt.evolution import (
    CandidateEvaluationError,
    EvaluationOutcome,
    SearchConfig,
    run_search_comparison,
)
from research.alphagpt.reward import RobustReward, RobustRewardConfig, TrainingFoldMetrics
from research.common.factor_dsl import StackVM
from research.factors.run_factor_search import TOP_DECILE, build_features, cross_sectional_score
from research.factors.run_factor_walkforward import (
    FULL0,
    FULL1,
    N_FOLDS,
    N_SYM,
    SEED,
    TRAIN_SKIP_TD,
    pl_scan,
)
from research.paths import FACTOR_ARTIFACTS_DIR, LOGS_DIR, ensure_artifact_dirs

OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_v1.json"
CHECKPOINT_DIR = LOGS_DIR / "alphagpt_v1"


@dataclass(frozen=True)
class FoldSpec:
    fold_id: str
    start: date
    end: date
    role: str

    def to_dict(self) -> dict[str, str]:
        return {
            "fold_id": self.fold_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "role": self.role,
        }


@dataclass
class FoldDataset:
    spec: FoldSpec
    features: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class _FoldEvaluation:
    mean_ic: float
    icir: float
    total_return: float
    turnover: float
    top_decile_sharpe: float
    correlation_signature: np.ndarray
    n_symbols: int
    n_observations: int


def fixed_fold_specs() -> tuple[list[FoldSpec], FoldSpec]:
    """沿用 P9 的日期范围与 4 块切分，最后一块永久封存为 holdout。"""

    all_dates = sorted(
        day
        for day in pl_scan().select("date").unique().collect()["date"].to_list()
        if FULL0 <= day <= FULL1
    )
    if len(all_dates) <= TRAIN_SKIP_TD + N_FOLDS:
        raise RuntimeError(f"交易日不足：{len(all_dates)}")
    remaining = all_dates[TRAIN_SKIP_TD:]
    chunk = len(remaining) // N_FOLDS
    blocks: list[tuple[date, date]] = []
    for index in range(N_FOLDS):
        start_index = index * chunk
        end_index = (index + 1) * chunk if index < N_FOLDS - 1 else len(remaining)
        dates = remaining[start_index:end_index]
        blocks.append((dates[0], dates[-1]))
    training = [
        FoldSpec(f"T{index + 1}", start, end, "train")
        for index, (start, end) in enumerate(blocks[:-1])
    ]
    holdout_start, holdout_end = blocks[-1]
    return training, FoldSpec("HOLDOUT", holdout_start, holdout_end, "holdout")


def select_universe(*, seed: int, universe_size: int, selection_end: date) -> list[str]:
    """只用训练期符号池抽样；固定排序避免 parquet 扫描顺序破坏复现。"""

    symbols = sorted(
        pl_scan()
        .filter((pl.col("date") >= FULL0) & (pl.col("date") <= selection_end))
        .select("symbol")
        .unique()
        .collect()["symbol"]
        .to_list()
    )
    rng = random.Random(seed)
    return sorted(rng.sample(symbols, min(universe_size, len(symbols))))


def load_fold_dataset(symbols: Sequence[str], spec: FoldSpec) -> FoldDataset:
    frame = (
        pl_scan()
        .filter(
            (pl.col("date") >= spec.start)
            & (pl.col("date") <= spec.end)
            & (pl.col("symbol").is_in(symbols))
        )
        .collect()
    )
    return FoldDataset(spec=spec, features=build_features(frame))


def _evaluate_formula_on_fold(
    tokens: Sequence[str],
    dataset: FoldDataset,
    *,
    signature_size: int = 4096,
) -> _FoldEvaluation:
    """执行 StackVM，并复用既有横截面评分函数计算训练折指标。"""

    symbols = sorted(dataset.features)
    if not symbols:
        raise CandidateEvaluationError(f"{dataset.spec.fold_id}: no usable symbols")
    periods = min(len(dataset.features[symbol]["close"]) for symbol in symbols)
    if periods < 20:
        raise CandidateEvaluationError(f"{dataset.spec.fold_id}: insufficient observations")

    signal_matrix = np.full((len(symbols), periods), np.nan)
    forward_returns = np.full((len(symbols), periods), np.nan)
    vm = StackVM()
    for index, symbol in enumerate(symbols):
        item = dataset.features[symbol]
        signal = vm.execute(tokens, item["feat"])
        if signal is None:
            raise CandidateEvaluationError(f"{dataset.spec.fold_id}: StackVM execution failed")
        signal_matrix[index] = np.asarray(signal, dtype=float)[-periods:]
        close = np.asarray(item["close"], dtype=float)[-periods:]
        returns = np.full(periods, np.nan)
        returns[1:] = close[1:] / close[:-1] - 1.0
        forward_returns[index] = returns

    finite_signal = np.nan_to_num(signal_matrix, nan=0.0, posinf=1.0, neginf=-1.0)
    if float(np.std(finite_signal)) <= 1e-12:
        raise CandidateEvaluationError(f"{dataset.spec.fold_id}: no signal (constant signal)")

    mean_ic, icir, sharpe = cross_sectional_score(finite_signal, forward_returns)
    metrics = np.asarray([mean_ic, icir, sharpe], dtype=float)
    if not np.all(np.isfinite(metrics)):
        raise CandidateEvaluationError(f"{dataset.spec.fold_id}: non-finite score")

    ranks = np.argsort(np.argsort(finite_signal, axis=0), axis=0) + 1.0
    selected = ranks >= (1.0 - TOP_DECILE) * len(symbols)
    portfolio_returns: list[float] = []
    for period in range(1, periods):
        values = forward_returns[selected[:, period - 1], period]
        values = values[np.isfinite(values)]
        if values.size:
            portfolio_returns.append(float(values.mean()))
    if not portfolio_returns:
        raise CandidateEvaluationError(f"{dataset.spec.fold_id}: no portfolio returns")
    total_return = float(np.prod(1.0 + np.asarray(portfolio_returns)) - 1.0)

    counts = selected.sum(axis=0, keepdims=True)
    weights = selected / np.maximum(counts, 1)
    if periods > 1:
        turnover = float(np.mean(0.5 * np.abs(np.diff(weights, axis=1)).sum(axis=0)))
    else:
        turnover = 0.0

    cross_sectional_mean = finite_signal.mean(axis=0, keepdims=True)
    cross_sectional_std = finite_signal.std(axis=0, keepdims=True) + 1e-9
    standardized = (finite_signal - cross_sectional_mean) / cross_sectional_std
    flattened = standardized.reshape(-1)
    stride = max(1, flattened.size // signature_size)
    signature = flattened[::stride][:signature_size]
    signature = np.nan_to_num(signature, nan=0.0, posinf=1.0, neginf=-1.0)
    if float(np.std(signature)) <= 1e-12:
        raise CandidateEvaluationError(f"{dataset.spec.fold_id}: no signal signature")

    values = np.asarray([total_return, turnover], dtype=float)
    if not np.all(np.isfinite(values)):
        raise CandidateEvaluationError(f"{dataset.spec.fold_id}: NaN or infinity")
    return _FoldEvaluation(
        mean_ic=float(mean_ic),
        icir=float(icir),
        total_return=total_return,
        turnover=turnover,
        top_decile_sharpe=float(sharpe),
        correlation_signature=signature,
        n_symbols=len(symbols),
        n_observations=periods,
    )


class TrainingEvaluator:
    """只持有训练折；构造函数没有 holdout 参数。"""

    def __init__(self, training_datasets: Sequence[FoldDataset]) -> None:
        if not training_datasets:
            raise ValueError("training datasets are required")
        if any(dataset.spec.role != "train" for dataset in training_datasets):
            raise ValueError("TrainingEvaluator cannot receive holdout/test datasets")
        self._training_datasets = tuple(training_datasets)

    def __call__(self, tokens: Sequence[str]) -> EvaluationOutcome:
        folds: list[TrainingFoldMetrics] = []
        signatures: list[np.ndarray] = []
        for dataset in self._training_datasets:
            result = _evaluate_formula_on_fold(tokens, dataset)
            folds.append(
                TrainingFoldMetrics(
                    fold_id=dataset.spec.fold_id,
                    start=dataset.spec.start.isoformat(),
                    end=dataset.spec.end.isoformat(),
                    mean_ic=result.mean_ic,
                    icir=result.icir,
                    total_return=result.total_return,
                    turnover=result.turnover,
                    top_decile_sharpe=result.top_decile_sharpe,
                )
            )
            signatures.append(result.correlation_signature)
        signature_length = min(len(signature) for signature in signatures)
        combined = np.concatenate([signature[:signature_length] for signature in signatures])
        return EvaluationOutcome(
            training_folds=tuple(folds),
            correlation_signal=combined,
            diagnostics={"source": "training_folds_only"},
        )


def evaluate_holdout(tokens: Sequence[str], dataset: FoldDataset) -> dict[str, Any]:
    """仅供搜索完成后的报告阶段调用，返回结构不能进入 RobustReward。"""

    if dataset.spec.role != "holdout":
        raise ValueError("evaluate_holdout requires the sealed holdout dataset")
    result = _evaluate_formula_on_fold(tokens, dataset)
    return {
        "fold_id": dataset.spec.fold_id,
        "start": dataset.spec.start.isoformat(),
        "end": dataset.spec.end.isoformat(),
        "dataset_role": "holdout_report_only",
        "mean_ic": result.mean_ic,
        "icir": result.icir,
        "total_return": result.total_return,
        "turnover": result.turnover,
        "top_decile_sharpe": result.top_decile_sharpe,
        "n_symbols": result.n_symbols,
        "n_observations": result.n_observations,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    ensure_artifact_dirs()
    training_specs, holdout_spec = fixed_fold_specs()
    universe = select_universe(
        seed=args.seed,
        universe_size=args.universe_size,
        selection_end=training_specs[-1].end,
    )
    training_data = [load_fold_dataset(universe, spec) for spec in training_specs]

    common_symbols = set(universe)
    for dataset in training_data:
        common_symbols &= set(dataset.features)
    common = sorted(common_symbols)
    if not common:
        raise RuntimeError("no symbols are common to every training fold")
    for dataset in training_data:
        dataset.features = {symbol: dataset.features[symbol] for symbol in common}
    data_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "symbols": common,
                "training_folds": [spec.to_dict() for spec in training_specs],
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    environment_config = AlphaEnvConfig(
        max_formula_length=args.max_formula_length,
        max_complexity=args.max_complexity,
        stop_probability=args.stop_probability,
        seed=args.seed,
    )
    search_config = SearchConfig(
        candidate_budget=args.candidate_budget,
        population_size=args.population_size,
        elite_size=args.elite_size,
        crossover_probability=args.crossover_probability,
        correlation_threshold=args.correlation_threshold,
        final_candidate_count=args.final_candidate_count,
        seed=args.seed,
        data_fingerprint=data_fingerprint,
    )
    reward = RobustReward(RobustRewardConfig(max_complexity=args.max_complexity))
    evaluator = TrainingEvaluator(training_data)
    searches = run_search_comparison(
        evaluator=evaluator,
        environment_config=environment_config,
        search_config=search_config,
        reward=reward,
        checkpoint_dir=CHECKPOINT_DIR,
        resume=args.resume,
    )

    # 排名已经冻结；现在才读取封存的 holdout，且不回写候选池或奖励。
    holdout_data = load_fold_dataset(common, holdout_spec)
    final_candidates: dict[str, list[dict[str, Any]]] = {}
    holdout_failures: list[dict[str, Any]] = []
    for method, result in searches.items():
        rows: list[dict[str, Any]] = []
        for rank, candidate in enumerate(result.final_candidates, start=1):
            row = {
                "selection_rank": rank,
                "selected_on": "training_reward_before_holdout",
                **candidate.to_dict(),
            }
            try:
                row["holdout_metrics"] = evaluate_holdout(candidate.tokens, holdout_data)
            except Exception as exc:
                failure = {
                    "reason": "holdout_evaluation_failure",
                    "method": method,
                    "formula": candidate.formula,
                    "formula_hash": candidate.formula_hash,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                }
                holdout_failures.append(failure)
                row["holdout_metrics"] = {"error": failure}
            rows.append(row)
        final_candidates[method] = rows

    search_payload = {
        method: result.to_dict()
        for method, result in searches.items()
    }
    candidate_lineage = [
        {"search_method": method, **candidate.to_dict()}
        for method, result in searches.items()
        for candidate in result.pool.candidates.values()
    ]
    failures = [
        {"search_method": method, **failure.to_dict()}
        for method, result in searches.items()
        for failure in result.pool.failures
    ]
    failures.extend(holdout_failures)
    payload = {
        "schema_version": 1,
        "phase": "P10 AlphaGPT closed loop v1",
        "config": {
            "seed": args.seed,
            "universe": {
                "requested_size": args.universe_size,
                "actual_size": len(common),
                "symbols": common,
                "selection": (
                    "training-period symbols only (through T3 end), sorted, then "
                    "random.Random(seed).sample; HOLDOUT availability is not consulted"
                ),
            },
            "full_date_range": {
                "start": FULL0.isoformat(),
                "end": FULL1.isoformat(),
            },
            "environment": asdict(environment_config),
            "search": asdict(search_config),
            "gpu_required": False,
            "transformer_or_ppo_used": False,
        },
        "walk_forward_folds": [
            *(spec.to_dict() for spec in training_specs),
            holdout_spec.to_dict(),
        ],
        "data_leakage_guard": {
            "search_evaluator": "TrainingEvaluator contains T1-T3 only",
            "reward_input": "TrainingFoldMetrics only",
            "holdout": (
                "sealed until both search rankings are frozen; report-only and never used "
                "for generation, selection, tuning, checkpoint decisions, or early stopping"
            ),
        },
        "reward_definition": reward.definition(),
        "budget_comparison": {
            method: {
                "evaluation_budget": result.evaluation_budget,
                "evaluations_used": result.evaluations_used,
            }
            for method, result in searches.items()
        },
        "searches": search_payload,
        "candidate_lineage": candidate_lineage,
        "failures": failures,
        "final_candidates": final_candidates,
        "known_limits": [
            "v1 uses CPU-only random/evolution policies; no learned token policy",
            "factor correlation uses a deterministic sampled cross-sectional z-score signature",
            "holdout metrics are descriptive and must not be used to revise this run's ranking",
        ],
    }
    temporary = OUT.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(OUT)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-budget", type=int, default=40)
    parser.add_argument("--population-size", type=int, default=10)
    parser.add_argument("--elite-size", type=int, default=4)
    parser.add_argument("--final-candidate-count", type=int, default=5)
    parser.add_argument("--universe-size", type=int, default=N_SYM)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--max-formula-length", type=int, default=10)
    parser.add_argument("--max-complexity", type=int, default=20)
    parser.add_argument("--stop-probability", type=float, default=0.18)
    parser.add_argument("--crossover-probability", type=float, default=0.45)
    parser.add_argument("--correlation-threshold", type=float, default=0.95)
    parser.add_argument("--resume", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = run(args)
    budget = payload["budget_comparison"]
    print(
        "[alphagpt-v1] complete | "
        f"random={budget['random']['evaluations_used']} "
        f"evolution={budget['evolution']['evaluations_used']} | {OUT}",
        flush=True,
    )


if __name__ == "__main__":
    main()
