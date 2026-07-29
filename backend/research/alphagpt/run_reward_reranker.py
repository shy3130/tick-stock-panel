"""P11-D：在全新候选池上前瞻验证公式 reward model reranker。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import date

import numpy as np

from research.alphagpt.environment import AlphaEnv, AlphaEnvConfig
from research.alphagpt.pool import FactorPool
from research.alphagpt.reranker import (
    ScoredFormula,
    generate_candidate_slate,
    random_baseline_selection,
    score_candidate_slate,
)
from research.alphagpt.reward import RobustReward, RobustRewardConfig
from research.alphagpt.reward_model import FormulaFeaturizer, RidgeRewardModel
from research.alphagpt.run_alphagpt_v1 import FoldSpec, TrainingEvaluator, load_fold_dataset
from research.paths import FACTOR_ARTIFACTS_DIR, ensure_artifact_dirs

P10 = FACTOR_ARTIFACTS_DIR / "alphagpt_v1.json"
MANIFEST = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_multiseed_v1_manifest.json"
MODEL = FACTOR_ARTIFACTS_DIR / "alphagpt_reward_model_v1.npz"
MODEL_REPORT = FACTOR_ARTIFACTS_DIR / "alphagpt_reward_model_v1.json"
REPORT_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_reward_reranker_v1.json"
DEFAULT_SEEDS = (20260811, 20260812, 20260813)


def _parse_seeds(value: str) -> tuple[int, ...]:
    seeds = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if len(seeds) < 2 or len(set(seeds)) != len(seeds):
        raise argparse.ArgumentTypeError("at least two unique seeds are required")
    return seeds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=_parse_seeds, default=DEFAULT_SEEDS)
    parser.add_argument("--slate-size", type=int, default=200)
    parser.add_argument("--evaluation-budget", type=int, default=20)
    return parser


def _evaluate_selection(
    selected: list[ScoredFormula],
    *,
    arm: str,
    evaluator: TrainingEvaluator,
    reward: RobustReward,
    correlation_threshold: float,
) -> dict:
    pool = FactorPool(correlation_threshold)
    evaluated: list[dict] = []
    for index, scored in enumerate(selected, start=1):
        candidate_id = f"{arm}_{index:06d}"
        try:
            outcome = evaluator(scored.tokens)
            correlation = pool.max_abs_correlation(outcome.correlation_signal)
            breakdown = reward.score(
                outcome.training_folds,
                complexity=AlphaEnv.formula_complexity(scored.tokens),
                max_abs_correlation=correlation[0],
            )
            add_result = pool.add_candidate(
                candidate_id=candidate_id,
                formula=scored.tokens,
                parent_formulas=[],
                generation_method=arm,
                complexity=AlphaEnv.formula_complexity(scored.tokens),
                fold_metrics=[fold.to_dict() for fold in outcome.training_folds],
                reward=breakdown.to_dict(),
                signal=outcome.correlation_signal,
                correlation=correlation,
            )
            evaluated.append(
                {
                    **scored.to_dict(),
                    "actual_training_reward": breakdown.total,
                    "pool_status": (
                        add_result.candidate.status
                        if add_result.candidate is not None
                        else "failed"
                    ),
                    "rejection_reason": add_result.reason,
                    "reward": breakdown.to_dict(),
                    "training_fold_metrics": [
                        fold.to_dict() for fold in outcome.training_folds
                    ],
                }
            )
        except Exception as exc:
            pool.record_failure(
                reason="evaluation_error",
                formula=scored.tokens,
                generation_method=arm,
                details={
                    "candidate_id": candidate_id,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            evaluated.append(
                {
                    **scored.to_dict(),
                    "actual_training_reward": None,
                    "pool_status": "failed",
                    "rejection_reason": "evaluation_error",
                }
            )
    realized = [
        float(candidate["actual_training_reward"])
        for candidate in evaluated
        if candidate["actual_training_reward"] is not None
    ]
    return {
        "evaluation_budget": len(selected),
        "evaluations_used": len(selected),
        "successful_evaluations": len(realized),
        "mean_training_reward": float(np.mean(realized)) if realized else None,
        "median_training_reward": float(np.median(realized)) if realized else None,
        "best_training_reward": max(realized) if realized else None,
        "positive_reward_ratio": (
            float(np.mean(np.asarray(realized) > 0)) if realized else None
        ),
        "n_pool_accepted": len(pool.accepted_candidates()),
        "failure_reasons": dict(Counter(failure.reason for failure in pool.failures)),
        "candidates": evaluated,
    }


def run(args: argparse.Namespace) -> dict:
    ensure_artifact_dirs()
    if args.slate_size < args.evaluation_budget or args.evaluation_budget <= 0:
        raise ValueError("slate size must be >= positive evaluation budget")

    reward_model_report = json.loads(MODEL_REPORT.read_text(encoding="utf-8"))
    if not reward_model_report["gate"]["passed"]:
        raise RuntimeError("reward model validation gate did not pass")
    model_sha = hashlib.sha256(MODEL.read_bytes()).hexdigest()
    if model_sha != reward_model_report["checkpoint"]["sha256"]:
        raise ValueError("reward model checkpoint hash does not match its report")

    p10 = json.loads(P10.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    model, feature_config = RidgeRewardModel.load(MODEL)
    expected_action_space = tuple(manifest["vocabulary"]["action_space"])
    if feature_config.action_space != expected_action_space:
        raise ValueError("reward model vocabulary does not match rollout manifest")
    environment_config = AlphaEnvConfig(**manifest["source"]["environment_config"])
    featurizer = FormulaFeaturizer(feature_config)

    training_specs = [
        FoldSpec(
            str(fold["fold_id"]),
            date.fromisoformat(fold["start"]),
            date.fromisoformat(fold["end"]),
            "train",
        )
        for fold in p10["walk_forward_folds"]
        if fold["role"] == "train"
    ]
    symbols = p10["config"]["universe"]["symbols"]
    evaluator = TrainingEvaluator(
        [load_fold_dataset(symbols, spec) for spec in training_specs]
    )
    reward = RobustReward(
        RobustRewardConfig(max_complexity=environment_config.max_complexity)
    )
    correlation_threshold = float(p10["config"]["search"]["correlation_threshold"])
    excluded_hashes = {
        str(episode["formula_hash"]) for episode in manifest["episodes"]
    }

    runs: list[dict] = []
    for seed in args.seeds:
        slate = generate_candidate_slate(
            environment_config=environment_config,
            seed=seed,
            count=args.slate_size,
            excluded_hashes=excluded_hashes,
        )
        scored = score_candidate_slate(
            slate,
            model=model,
            featurizer=featurizer,
        )
        reranked = scored[: args.evaluation_budget]
        random_selected = random_baseline_selection(
            scored,
            count=args.evaluation_budget,
            seed=seed + 100000,
        )
        reranker_result = _evaluate_selection(
            reranked,
            arm=f"reranker_seed_{seed}",
            evaluator=evaluator,
            reward=reward,
            correlation_threshold=correlation_threshold,
        )
        random_result = _evaluate_selection(
            random_selected,
            arm=f"random_seed_{seed}",
            evaluator=evaluator,
            reward=reward,
            correlation_threshold=correlation_threshold,
        )
        runs.append(
            {
                "seed": seed,
                "candidate_slate": {
                    "size": len(scored),
                    "excluded_training_or_validation_formulas": len(excluded_hashes),
                    "sha256": hashlib.sha256(
                        json.dumps(
                            [item.formula_hash for item in scored],
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                },
                "reranker": reranker_result,
                "random": random_result,
                "paired_mean_reward_difference": (
                    float(reranker_result["mean_training_reward"])
                    - float(random_result["mean_training_reward"])
                    if reranker_result["mean_training_reward"] is not None
                    and random_result["mean_training_reward"] is not None
                    else None
                ),
            }
        )

    reranker_means = [
        float(run["reranker"]["mean_training_reward"])
        for run in runs
        if run["reranker"]["mean_training_reward"] is not None
    ]
    random_means = [
        float(run["random"]["mean_training_reward"])
        for run in runs
        if run["random"]["mean_training_reward"] is not None
    ]
    differences = [
        float(run["paired_mean_reward_difference"])
        for run in runs
        if run["paired_mean_reward_difference"] is not None
    ]
    wins = sum(difference > 0 for difference in differences)
    checks = {
        "identical_evaluation_budget": all(
            run["reranker"]["evaluations_used"]
            == run["random"]["evaluations_used"]
            == args.evaluation_budget
            for run in runs
        ),
        "all_runs_have_at_least_90pct_successful_evaluations": all(
            run[arm]["successful_evaluations"] >= 0.9 * args.evaluation_budget
            for run in runs
            for arm in ("reranker", "random")
        ),
        "reranker_aggregate_mean_reward_positive": float(np.mean(reranker_means)) > 0,
        "paired_mean_difference_positive": float(np.mean(differences)) > 0,
        "reranker_wins_majority_of_seeds": wins >= len(runs) // 2 + 1,
    }
    gate_passed = all(checks.values())
    report = {
        "schema_version": 1,
        "phase": "P11-D prospective formula reward reranker v1",
        "config": {
            "seeds": list(args.seeds),
            "slate_size_per_seed": args.slate_size,
            "evaluation_budget_per_arm_per_seed": args.evaluation_budget,
            "candidate_generator": "AlphaEnv random legal formula sampler",
            "reranker": "top predicted training reward",
            "baseline": "deterministic random sample from identical candidate slate",
            "gpu_required": False,
        },
        "data": {
            "universe": symbols,
            "training_folds": [spec.to_dict() for spec in training_specs],
            "boundary": "all selection and realized rewards use T1-T3; HOLDOUT is never loaded",
            "reward_model_checkpoint": MODEL.name,
            "reward_model_checkpoint_sha256": model_sha,
        },
        "leakage_controls": {
            "candidate_generation": "new seeds and formula hashes absent from rollout manifest",
            "selection_inputs": "formula tokens and frozen reward-model predictions only",
            "realized_reward_usage": "post-selection comparison only",
            "baseline_budget": "same candidate slate and exact evaluator-call budget",
            "holdout_usage": "none; TrainingEvaluator rejects non-training datasets",
        },
        "runs": runs,
        "aggregate": {
            "reranker_mean_training_reward": float(np.mean(reranker_means)),
            "random_mean_training_reward": float(np.mean(random_means)),
            "paired_mean_reward_difference": float(np.mean(differences)),
            "reranker_seed_wins": wins,
            "n_seeds": len(runs),
        },
        "gate": {
            "checks": checks,
            "passed": gate_passed,
            "decision": (
                "eligible for reward-model-guided policy integration design"
                if gate_passed
                else "do not integrate reranker into policy search"
            ),
        },
        "known_limits": [
            "realized rewards remain training-fold rewards, not new OOS evidence",
            "three prospective seeds are a small comparison sample",
            "random baseline and reranker share candidate slates but may select overlapping formulas",
            "PPO remains out of scope until a separate design and leakage review",
        ],
    }
    temporary = REPORT_OUT.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(REPORT_OUT)
    return report


def main() -> None:
    args = build_parser().parse_args()
    report = run(args)
    print(
        "[alphagpt-reward-reranker] complete | "
        f"gate={'PASS' if report['gate']['passed'] else 'FAIL'} "
        f"reranker={report['aggregate']['reranker_mean_training_reward']:+.3f} "
        f"random={report['aggregate']['random_mean_training_reward']:+.3f} "
        f"| {REPORT_OUT}",
        flush=True,
    )


if __name__ == "__main__":
    main()
