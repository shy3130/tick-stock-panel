"""P11-C 前置：在扩容 rollout 上重复训练多个模型 seed，审计稳定性。"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import date

import numpy as np

from research.alphagpt.behavior_clone import (
    NGramBehaviorPolicy,
    NumpyMaskedTransformer,
    TinyTransformerConfig,
    load_behavior_examples,
)
from research.alphagpt.environment import AlphaEnvConfig
from research.alphagpt.policy import MaskedLogitPolicy
from research.alphagpt.reward import RobustReward, RobustRewardConfig
from research.alphagpt.run_alphagpt_v1 import FoldSpec, TrainingEvaluator, load_fold_dataset
from research.alphagpt.run_behavior_clone import _evaluate_policy, _generation_metrics
from research.paths import FACTOR_ARTIFACTS_DIR, ensure_artifact_dirs

DATASET = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_multiseed_v1.jsonl"
MANIFEST = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_multiseed_v1_manifest.json"
P10 = FACTOR_ARTIFACTS_DIR / "alphagpt_v1.json"
REPORT_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_bc_stability_v1.json"
DEFAULT_MODEL_SEEDS = (20260731, 20260732, 20260733)


def _parse_seeds(value: str) -> tuple[int, ...]:
    seeds = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if len(seeds) < 2 or len(set(seeds)) != len(seeds):
        raise argparse.ArgumentTypeError("at least two unique model seeds are required")
    return seeds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-seeds", type=_parse_seeds, default=DEFAULT_MODEL_SEEDS)
    parser.add_argument("--epochs", type=int, default=250)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--d-model", type=int, default=24)
    parser.add_argument("--learning-rate", type=float, default=0.01)
    parser.add_argument("--early-stopping-patience", type=int, default=40)
    parser.add_argument("--generation-samples", type=int, default=500)
    parser.add_argument("--evaluation-budget", type=int, default=20)
    parser.add_argument("--temperature", type=float, default=0.8)
    return parser


def _aggregate(values: list[float]) -> dict[str, float]:
    return {
        "mean": float(np.mean(values)),
        "std": float(np.std(values)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
    }


def run(args: argparse.Namespace) -> dict:
    ensure_artifact_dirs()
    dataset_bytes = DATASET.read_bytes()
    dataset_sha = hashlib.sha256(dataset_bytes).hexdigest()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if dataset_sha != manifest["dataset_sha256"]:
        raise ValueError("expanded rollout hash does not match manifest")
    examples = load_behavior_examples(DATASET)
    train = [example for example in examples if example.split == "train"]
    validation = [example for example in examples if example.split == "validation"]
    action_size = len(manifest["vocabulary"]["action_space"])
    environment_config = AlphaEnvConfig(**manifest["source"]["environment_config"])

    ngram = NGramBehaviorPolicy(action_size=action_size, order=2)
    ngram.fit(train)
    ngram_validation = ngram.evaluate(validation)

    p10 = json.loads(P10.read_text(encoding="utf-8"))
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

    runs: list[dict] = []
    for seed in args.model_seeds:
        config = TinyTransformerConfig(
            action_size=action_size,
            max_prefix_length=max(len(example.token_ids) for example in examples),
            d_model=args.d_model,
            seed=seed,
            learning_rate=args.learning_rate,
        )
        model = NumpyMaskedTransformer(config)
        training = model.train(
            train,
            validation,
            epochs=args.epochs,
            batch_size=args.batch_size,
            early_stopping_patience=args.early_stopping_patience,
        )
        checkpoint = FACTOR_ARTIFACTS_DIR / f"alphagpt_bc_stability_seed_{seed}.npz"
        model.save(checkpoint)
        checkpoint_sha = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
        restored = NumpyMaskedTransformer.load(checkpoint)
        generation = _generation_metrics(
            MaskedLogitPolicy(
                restored.logits,
                seed=seed,
                temperature=args.temperature,
                name=f"transformer_seed_{seed}",
            ),
            environment_config,
            count=args.generation_samples,
            seed=seed,
        )
        reward_evaluation = _evaluate_policy(
            policy=MaskedLogitPolicy(
                restored.logits,
                seed=seed + 1000,
                temperature=args.temperature,
                name=f"transformer_seed_{seed}",
            ),
            policy_name=f"transformer_seed_{seed}",
            environment_config=environment_config,
            evaluator=evaluator,
            reward=reward,
            candidate_budget=args.evaluation_budget,
            correlation_threshold=correlation_threshold,
            seed=seed + 1000,
        )
        runs.append(
            {
                "seed": seed,
                "model_config": asdict(config),
                "training": training,
                "checkpoint": checkpoint.name,
                "checkpoint_sha256": checkpoint_sha,
                "generation": generation,
                "training_reward_evaluation": reward_evaluation,
            }
        )

    validation_nll = [
        float(run["training"]["final"]["validation"]["nll"]) for run in runs
    ]
    validation_accuracy = [
        float(run["training"]["final"]["validation"]["accuracy"]) for run in runs
    ]
    unique_rates = [float(run["generation"]["unique_rate"]) for run in runs]
    mean_rewards = [
        float(run["training_reward_evaluation"]["mean_reward"]) for run in runs
    ]
    gate_checks = {
        "all_valid_formula_rate_100pct": all(
            run["generation"]["valid_formula_rate"] == 1.0 for run in runs
        ),
        "minimum_unique_rate_at_least_25pct": min(unique_rates) >= 0.25,
        "validation_accuracy_std_at_most_10pct": float(np.std(validation_accuracy)) <= 0.10,
        "positive_mean_reward_in_majority_of_seeds": (
            sum(value > 0 for value in mean_rewards) >= len(mean_rewards) // 2 + 1
        ),
    }
    gate_passed = all(gate_checks.values())
    report = {
        "schema_version": 1,
        "phase": "P11-C pre-PPO multiseed stability v1",
        "config": {
            "model_seeds": list(args.model_seeds),
            "epochs": args.epochs,
            "batch_size": args.batch_size,
            "d_model": args.d_model,
            "learning_rate": args.learning_rate,
            "early_stopping_patience": args.early_stopping_patience,
            "generation_samples_per_seed": args.generation_samples,
            "evaluation_budget_per_seed": args.evaluation_budget,
            "temperature": args.temperature,
            "gpu_required": False,
        },
        "data": {
            "dataset": "artifacts/archive/factors/alphagpt_rollouts_multiseed_v1.jsonl",
            "dataset_sha256": dataset_sha,
            "train_transitions": len(train),
            "validation_transitions": len(validation),
            "expansion_seeds": manifest["source"]["expansion_seeds"],
            "boundary": "T1-T3 training data only; P10 HOLDOUT is not loaded",
        },
        "ngram_validation": ngram_validation,
        "runs": runs,
        "aggregate": {
            "validation_nll": _aggregate(validation_nll),
            "validation_accuracy": _aggregate(validation_accuracy),
            "generation_unique_rate": _aggregate(unique_rates),
            "mean_training_reward": _aggregate(mean_rewards),
        },
        "pre_ppo_gate": {
            "checks": gate_checks,
            "passed": gate_passed,
            "decision": (
                "eligible for PPO design review"
                if gate_passed
                else "not ready for PPO; expand data or reduce policy collapse"
            ),
        },
        "known_limits": [
            "four evolution data seeds and three model seeds remain a small stability sample",
            "reward evaluation uses training folds only and is not an OOS claim",
            "P10 HOLDOUT has already been consumed and is excluded from this stage",
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
    gate = report["pre_ppo_gate"]
    print(
        "[alphagpt-stability] complete | "
        f"gate={'PASS' if gate['passed'] else 'FAIL'} "
        f"mean_reward={report['aggregate']['mean_training_reward']['mean']:+.3f} "
        f"| {REPORT_OUT}",
        flush=True,
    )


if __name__ == "__main__":
    main()
