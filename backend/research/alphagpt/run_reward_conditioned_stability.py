"""P11-C2：reward-weighted 与 elite BC 的多 seed 稳定性 gate。"""

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
    prepare_reward_conditioned_examples,
)
from research.alphagpt.environment import AlphaEnvConfig
from research.alphagpt.policy import MaskedLogitPolicy
from research.alphagpt.reward import RobustReward, RobustRewardConfig
from research.alphagpt.run_alphagpt_v1 import FoldSpec, TrainingEvaluator, load_fold_dataset
from research.alphagpt.run_behavior_clone import _evaluate_policy, _generation_metrics
from research.alphagpt.run_behavior_stability import _aggregate, _parse_seeds
from research.paths import FACTOR_ARTIFACTS_DIR, ensure_artifact_dirs

DATASET = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_multiseed_v1.jsonl"
MANIFEST = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_multiseed_v1_manifest.json"
P10 = FACTOR_ARTIFACTS_DIR / "alphagpt_v1.json"
UNIFORM_BASELINE = FACTOR_ARTIFACTS_DIR / "alphagpt_bc_stability_v1.json"
REPORT_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_reward_conditioned_stability_v1.json"
DEFAULT_MODEL_SEEDS = (20260801, 20260802, 20260803)
MODES = ("reward_weighted", "elite")


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
    parser.add_argument("--reward-temperature", type=float, default=2.0)
    parser.add_argument("--elite-quantile", type=float, default=0.60)
    return parser


def _mode_gate(runs: list[dict]) -> dict:
    validation_accuracy = [
        float(run["training"]["final"]["validation"]["accuracy"]) for run in runs
    ]
    unique_rates = [float(run["generation"]["unique_rate"]) for run in runs]
    mean_rewards = [
        float(run["training_reward_evaluation"]["mean_reward"]) for run in runs
    ]
    checks = {
        "all_valid_formula_rate_100pct": all(
            run["generation"]["valid_formula_rate"] == 1.0 for run in runs
        ),
        "minimum_unique_rate_at_least_25pct": min(unique_rates) >= 0.25,
        "validation_accuracy_std_at_most_10pct": float(np.std(validation_accuracy)) <= 0.10,
        "positive_mean_reward_in_majority_of_seeds": (
            sum(value > 0 for value in mean_rewards) >= len(mean_rewards) // 2 + 1
        ),
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "positive_reward_seeds": sum(value > 0 for value in mean_rewards),
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
    evaluator = TrainingEvaluator(
        [
            load_fold_dataset(p10["config"]["universe"]["symbols"], spec)
            for spec in training_specs
        ]
    )
    reward = RobustReward(
        RobustRewardConfig(max_complexity=environment_config.max_complexity)
    )
    correlation_threshold = float(p10["config"]["search"]["correlation_threshold"])

    mode_results: dict[str, dict] = {}
    for mode_index, mode in enumerate(MODES):
        conditioned, conditioning_audit = prepare_reward_conditioned_examples(
            train,
            mode=mode,
            elite_quantile=args.elite_quantile,
            reward_temperature=args.reward_temperature,
        )
        ngram = NGramBehaviorPolicy(action_size=action_size, order=2)
        ngram.fit(conditioned)
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
                conditioned,
                validation,
                epochs=args.epochs,
                batch_size=args.batch_size,
                early_stopping_patience=args.early_stopping_patience,
            )
            checkpoint = (
                FACTOR_ARTIFACTS_DIR / f"alphagpt_bc_{mode}_seed_{seed}.npz"
            )
            model.save(checkpoint)
            restored = NumpyMaskedTransformer.load(checkpoint)
            generation_seed = seed + mode_index * 10000
            generation = _generation_metrics(
                MaskedLogitPolicy(
                    restored.logits,
                    seed=generation_seed,
                    temperature=args.temperature,
                    name=f"{mode}_seed_{seed}",
                ),
                environment_config,
                count=args.generation_samples,
                seed=generation_seed,
            )
            reward_evaluation = _evaluate_policy(
                policy=MaskedLogitPolicy(
                    restored.logits,
                    seed=generation_seed + 1000,
                    temperature=args.temperature,
                    name=f"{mode}_seed_{seed}",
                ),
                policy_name=f"{mode}_seed_{seed}",
                environment_config=environment_config,
                evaluator=evaluator,
                reward=reward,
                candidate_budget=args.evaluation_budget,
                correlation_threshold=correlation_threshold,
                seed=generation_seed + 1000,
            )
            runs.append(
                {
                    "seed": seed,
                    "model_config": asdict(config),
                    "training": training,
                    "checkpoint": checkpoint.name,
                    "checkpoint_sha256": hashlib.sha256(
                        checkpoint.read_bytes()
                    ).hexdigest(),
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
        mode_results[mode] = {
            "conditioning": conditioning_audit,
            "ngram_validation": ngram.evaluate(validation),
            "runs": runs,
            "aggregate": {
                "validation_nll": _aggregate(validation_nll),
                "validation_accuracy": _aggregate(validation_accuracy),
                "generation_unique_rate": _aggregate(unique_rates),
                "mean_training_reward": _aggregate(mean_rewards),
            },
            "gate": _mode_gate(runs),
        }

    passing_modes = [
        mode for mode, result in mode_results.items() if result["gate"]["passed"]
    ]
    uniform_baseline = json.loads(UNIFORM_BASELINE.read_text(encoding="utf-8"))
    report = {
        "schema_version": 1,
        "phase": "P11-C2 reward-conditioned behavior cloning v1",
        "config": {
            "modes": list(MODES),
            "model_seeds": list(args.model_seeds),
            "epochs": args.epochs,
            "batch_size": args.batch_size,
            "d_model": args.d_model,
            "learning_rate": args.learning_rate,
            "early_stopping_patience": args.early_stopping_patience,
            "generation_samples_per_seed": args.generation_samples,
            "evaluation_budget_per_seed": args.evaluation_budget,
            "temperature": args.temperature,
            "reward_temperature": args.reward_temperature,
            "elite_quantile": args.elite_quantile,
            "gpu_required": False,
        },
        "data": {
            "dataset": "artifacts/archive/factors/alphagpt_rollouts_multiseed_v1.jsonl",
            "dataset_sha256": dataset_sha,
            "train_transitions": len(train),
            "validation_transitions": len(validation),
            "boundary": "conditioning and evaluation use T1-T3 training information only",
        },
        "uniform_bc_baseline": {
            "aggregate": uniform_baseline["aggregate"],
            "gate": uniform_baseline["pre_ppo_gate"],
        },
        "modes": mode_results,
        "pre_ppo_gate": {
            "passed": bool(passing_modes),
            "passing_modes": passing_modes,
            "decision": (
                "eligible for PPO design review"
                if passing_modes
                else "not ready for PPO; reward-conditioned BC is not stable"
            ),
        },
        "known_limits": [
            "all rewards are training-fold rewards and are not OOS evidence",
            "three model seeds remain a small stability sample",
            "P10 HOLDOUT is excluded from conditioning, early stopping and evaluation",
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
        "[alphagpt-reward-conditioned] complete | "
        f"gate={'PASS' if gate['passed'] else 'FAIL'} "
        f"passing={gate['passing_modes']} | {REPORT_OUT}",
        flush=True,
    )


if __name__ == "__main__":
    main()
