"""P11-E：用全新随机公式 seed 采集分布对齐的 T1–T3 奖励标签。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from datetime import date

import numpy as np

from research.alphagpt.environment import AlphaEnv, AlphaEnvConfig
from research.alphagpt.pool import FactorPool, formula_hash
from research.alphagpt.reranker import generate_candidate_slate
from research.alphagpt.reward import RobustReward, RobustRewardConfig
from research.alphagpt.reward_labels import write_json_atomic
from research.alphagpt.run_alphagpt_v1 import FoldSpec, TrainingEvaluator, load_fold_dataset
from research.paths import FACTOR_ARTIFACTS_DIR, ensure_artifact_dirs

P10 = FACTOR_ARTIFACTS_DIR / "alphagpt_v1.json"
OLD_MANIFEST = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_multiseed_v1_manifest.json"
P11D_RERANKER = FACTOR_ARTIFACTS_DIR / "alphagpt_reward_reranker_v1.json"
OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_reward_labels_v2.json"
DEFAULT_TRAIN_SEEDS = (20260821, 20260822, 20260823, 20260824)
DEFAULT_VALIDATION_SEEDS = (20260825, 20260826)


def _parse_seeds(value: str) -> tuple[int, ...]:
    seeds = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not seeds or len(seeds) != len(set(seeds)):
        raise argparse.ArgumentTypeError("seeds must be a non-empty unique list")
    return seeds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-seeds", type=_parse_seeds, default=DEFAULT_TRAIN_SEEDS)
    parser.add_argument(
        "--validation-seeds",
        type=_parse_seeds,
        default=DEFAULT_VALIDATION_SEEDS,
    )
    parser.add_argument("--candidate-budget", type=int, default=40)
    return parser


def _excluded_formula_hashes() -> set[str]:
    manifest = json.loads(OLD_MANIFEST.read_text(encoding="utf-8"))
    excluded = {str(episode["formula_hash"]) for episode in manifest["episodes"]}
    if P11D_RERANKER.exists():
        report = json.loads(P11D_RERANKER.read_text(encoding="utf-8"))
        for run in report["runs"]:
            for arm in ("reranker", "random"):
                excluded.update(
                    str(candidate["formula_hash"])
                    for candidate in run[arm]["candidates"]
                )
    return excluded


def run(args: argparse.Namespace) -> dict:
    ensure_artifact_dirs()
    if args.candidate_budget <= 0:
        raise ValueError("candidate budget must be > 0")
    if set(args.train_seeds) & set(args.validation_seeds):
        raise ValueError("train and validation seeds must be disjoint")

    p10_bytes = P10.read_bytes()
    p10 = json.loads(p10_bytes)
    environment_config = AlphaEnvConfig(**p10["config"]["environment"])
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
    symbols = list(p10["config"]["universe"]["symbols"])
    evaluator = TrainingEvaluator(
        [load_fold_dataset(symbols, spec) for spec in training_specs]
    )
    reward = RobustReward(
        RobustRewardConfig(max_complexity=environment_config.max_complexity)
    )
    correlation_threshold = float(p10["config"]["search"]["correlation_threshold"])
    excluded = _excluded_formula_hashes()
    initial_excluded_count = len(excluded)

    labels: list[dict] = []
    failures: list[dict] = []
    per_seed: list[dict] = []
    for split, seeds in (
        ("train", args.train_seeds),
        ("validation", args.validation_seeds),
    ):
        for seed in seeds:
            slate = generate_candidate_slate(
                environment_config=environment_config,
                seed=seed,
                count=args.candidate_budget,
                excluded_hashes=excluded,
            )
            pool = FactorPool(correlation_threshold)
            seed_labels = 0
            for index, tokens in enumerate(slate, start=1):
                digest = formula_hash(tokens)
                excluded.add(digest)
                try:
                    outcome = evaluator(tokens)
                    complexity = AlphaEnv.formula_complexity(tokens)
                    intrinsic = reward.score(
                        outcome.training_folds,
                        complexity=complexity,
                        max_abs_correlation=0.0,
                    )
                    correlation = pool.max_abs_correlation(outcome.correlation_signal)
                    operational = reward.score(
                        outcome.training_folds,
                        complexity=complexity,
                        max_abs_correlation=correlation[0],
                    )
                    add_result = pool.add_candidate(
                        candidate_id=f"p11e_s{seed}_{index:04d}",
                        formula=tokens,
                        parent_formulas=[],
                        generation_method="random_legal_formula",
                        complexity=complexity,
                        fold_metrics=[
                            fold.to_dict() for fold in outcome.training_folds
                        ],
                        reward=operational.to_dict(),
                        signal=outcome.correlation_signal,
                        correlation=correlation,
                    )
                    labels.append(
                        {
                            "formula_hash": digest,
                            "formula_tokens": list(tokens),
                            "formula": " ".join(tokens),
                            "split": split,
                            "data_seed": seed,
                            "candidate_index": index,
                            "generation_method": "AlphaEnv random legal formula sampler",
                            "complexity": complexity,
                            "intrinsic_reward": intrinsic.total,
                            "operational_reward": operational.total,
                            "intrinsic_reward_breakdown": intrinsic.to_dict(),
                            "operational_reward_breakdown": operational.to_dict(),
                            "pool_status": (
                                add_result.candidate.status
                                if add_result.candidate is not None
                                else "failed"
                            ),
                            "rejection_reason": add_result.reason,
                            "training_fold_metrics": [
                                fold.to_dict() for fold in outcome.training_folds
                            ],
                        }
                    )
                    seed_labels += 1
                except Exception as exc:
                    failures.append(
                        {
                            "formula_hash": digest,
                            "formula_tokens": list(tokens),
                            "split": split,
                            "data_seed": seed,
                            "candidate_index": index,
                            "reason": "evaluation_error",
                            "exception_type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
            seed_rewards = [
                float(label["intrinsic_reward"])
                for label in labels
                if label["data_seed"] == seed
            ]
            per_seed.append(
                {
                    "seed": seed,
                    "split": split,
                    "evaluation_budget": args.candidate_budget,
                    "successful_labels": seed_labels,
                    "failures": args.candidate_budget - seed_labels,
                    "intrinsic_reward_mean": (
                        float(np.mean(seed_rewards)) if seed_rewards else None
                    ),
                    "intrinsic_positive_ratio": (
                        float(np.mean(np.asarray(seed_rewards) > 0))
                        if seed_rewards
                        else None
                    ),
                }
            )

    train_labels = [label for label in labels if label["split"] == "train"]
    validation_labels = [
        label for label in labels if label["split"] == "validation"
    ]
    payload = {
        "schema_version": 2,
        "phase": "P11-E random formula reward labels v2",
        "config": {
            "train_seeds": list(args.train_seeds),
            "validation_seeds": list(args.validation_seeds),
            "candidate_budget_per_seed": args.candidate_budget,
            "environment": asdict(environment_config),
            "correlation_threshold": correlation_threshold,
            "gpu_required": False,
        },
        "data": {
            "source_p10_sha256": hashlib.sha256(p10_bytes).hexdigest(),
            "universe": symbols,
            "training_folds": [spec.to_dict() for spec in training_specs],
            "initial_excluded_formula_hashes": initial_excluded_count,
            "boundary": "T1-T3 only; HOLDOUT is never loaded",
        },
        "target_definition": {
            "model_target": "intrinsic_reward",
            "intrinsic_reward": (
                "RobustReward on T1-T3 with max_abs_correlation fixed to zero"
            ),
            "operational_reward": (
                "same reward plus correlation to earlier accepted candidates "
                "within the same seed"
            ),
            "reason": (
                "formula-only model cannot observe future candidate-pool state"
            ),
            "reward_definition": reward.definition(),
        },
        "counts": {
            "labels": len(labels),
            "train_labels": len(train_labels),
            "validation_labels": len(validation_labels),
            "failures": len(failures),
            "failure_reasons": dict(Counter(item["reason"] for item in failures)),
        },
        "per_seed": per_seed,
        "labels": labels,
        "failures": failures,
        "leakage_controls": {
            "split_unit": "data seed, not individual formula",
            "train_validation_seeds_disjoint": True,
            "old_rollout_and_p11d_formulas_excluded": True,
            "holdout_usage": "none",
        },
    }
    write_json_atomic(OUT, payload)
    return payload


def main() -> None:
    args = build_parser().parse_args()
    payload = run(args)
    print(
        "[alphagpt-reward-labels] complete | "
        f"train={payload['counts']['train_labels']} "
        f"validation={payload['counts']['validation_labels']} "
        f"failures={payload['counts']['failures']} | {OUT}",
        flush=True,
    )


if __name__ == "__main__":
    main()
