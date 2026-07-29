"""P11-B：训练纯 NumPy masked Transformer，并与 n-gram/random 生成基线对比。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from datetime import date

import numpy as np

from research.alphagpt.behavior_clone import (
    NGramBehaviorPolicy,
    NumpyMaskedTransformer,
    TinyTransformerConfig,
    load_behavior_examples,
)
from research.alphagpt.environment import AlphaEnv, AlphaEnvConfig
from research.alphagpt.pool import FactorPool, formula_hash
from research.alphagpt.policy import MaskedLogitPolicy, RandomTokenPolicy
from research.alphagpt.reward import RobustReward, RobustRewardConfig
from research.alphagpt.rollouts import run_policy_episode
from research.alphagpt.run_alphagpt_v1 import FoldSpec, TrainingEvaluator, load_fold_dataset
from research.paths import FACTOR_ARTIFACTS_DIR, ensure_artifact_dirs

DATASET = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_v1.jsonl"
MANIFEST = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_v1_manifest.json"
P10 = FACTOR_ARTIFACTS_DIR / "alphagpt_v1.json"
MODEL_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_bc_v1.npz"
REPORT_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_bc_v1.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epochs", type=int, default=250)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--d-model", type=int, default=24)
    parser.add_argument("--learning-rate", type=float, default=0.01)
    parser.add_argument("--early-stopping-patience", type=int, default=40)
    parser.add_argument("--generation-samples", type=int, default=1000)
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--evaluation-budget", type=int, default=40)
    parser.add_argument("--seed", type=int, default=20260724)
    return parser


def _generation_metrics(policy, environment_config, *, count: int, seed: int):
    formulas: list[tuple[str, ...]] = []
    complexities: list[int] = []
    for index in range(count):
        episode = run_policy_episode(
            policy=policy,
            environment_config=environment_config,
            episode_id=f"generation_{index}",
            seed=seed + index,
        )
        formulas.append(episode.formula_tokens)
        complexities.append(AlphaEnv.formula_complexity(episode.formula_tokens))
    return {
        "n_formulas": count,
        "valid_formula_rate": 1.0,
        "unique_formulas": len(set(formulas)),
        "unique_rate": len(set(formulas)) / count,
        "mean_formula_length": float(np.mean([len(formula) for formula in formulas])),
        "mean_complexity_proxy": float(np.mean(complexities)),
    }


def _reward_summary(candidates) -> dict:
    rewards = [float(candidate.reward["total"]) for candidate in candidates]
    return {
        "n_candidates": len(candidates),
        "mean_reward": float(np.mean(rewards)) if rewards else None,
        "median_reward": float(np.median(rewards)) if rewards else None,
        "best_reward": max(rewards) if rewards else None,
    }


def _source_search_summary(payload: dict, method: str) -> dict:
    search = payload["searches"][method]
    accepted = [
        candidate
        for candidate in search["pool"]["candidates"]
        if candidate["status"] == "accepted"
    ]
    rewards = [float(candidate["reward"]["total"]) for candidate in accepted]
    return {
        "evaluation_budget": search["evaluation_budget"],
        "evaluations_used": search["evaluations_used"],
        "n_accepted": len(accepted),
        "mean_reward": float(np.mean(rewards)) if rewards else None,
        "median_reward": float(np.median(rewards)) if rewards else None,
        "best_reward": max(rewards) if rewards else None,
        "source": "P10 training candidate pool",
    }


def _evaluate_policy(
    *,
    policy,
    policy_name: str,
    environment_config: AlphaEnvConfig,
    evaluator: TrainingEvaluator,
    reward: RobustReward,
    candidate_budget: int,
    correlation_threshold: float,
    seed: int,
) -> dict:
    pool = FactorPool(correlation_threshold)
    attempted: set[str] = set()
    evaluations = 0
    generation_attempts = 0
    while evaluations < candidate_budget:
        generation_attempts += 1
        if generation_attempts > candidate_budget * 500:
            raise RuntimeError(f"{policy_name} could not generate enough unique formulas")
        episode = run_policy_episode(
            policy=policy,
            environment_config=environment_config,
            episode_id=f"{policy_name}_{generation_attempts}",
            seed=seed + generation_attempts,
        )
        digest = formula_hash(episode.formula_tokens)
        if digest in attempted:
            pool.record_failure(
                reason="duplicate_formula",
                formula=episode.formula_tokens,
                generation_method=policy_name,
                details={"stage": "generation"},
            )
            continue
        attempted.add(digest)
        evaluations += 1
        candidate_id = f"{policy_name}_{evaluations:06d}"
        try:
            outcome = evaluator(episode.formula_tokens)
            correlation = pool.max_abs_correlation(outcome.correlation_signal)
            breakdown = reward.score(
                outcome.training_folds,
                complexity=AlphaEnv.formula_complexity(episode.formula_tokens),
                max_abs_correlation=correlation[0],
            )
            pool.add_candidate(
                candidate_id=candidate_id,
                formula=episode.formula_tokens,
                parent_formulas=[],
                generation_method=policy_name,
                complexity=AlphaEnv.formula_complexity(episode.formula_tokens),
                fold_metrics=[fold.to_dict() for fold in outcome.training_folds],
                reward=breakdown.to_dict(),
                signal=outcome.correlation_signal,
                correlation=correlation,
            )
        except Exception as exc:
            pool.record_failure(
                reason="evaluation_error",
                formula=episode.formula_tokens,
                generation_method=policy_name,
                details={
                    "candidate_id": candidate_id,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
    accepted = pool.ranked_candidates()
    summary = _reward_summary(accepted)
    return {
        "evaluation_budget": candidate_budget,
        "evaluations_used": evaluations,
        "generation_attempts": generation_attempts,
        "n_accepted": len(accepted),
        **{key: value for key, value in summary.items() if key != "n_candidates"},
        "failure_reasons": dict(Counter(failure.reason for failure in pool.failures)),
        "top_candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "formula": candidate.formula,
                "formula_hash": candidate.formula_hash,
                "reward": candidate.reward,
                "fold_metrics": candidate.fold_metrics,
            }
            for candidate in accepted[:5]
        ],
        "pool": pool.to_dict(),
    }


def run(args: argparse.Namespace) -> dict:
    ensure_artifact_dirs()
    dataset_bytes = DATASET.read_bytes()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    actual_dataset_sha = hashlib.sha256(dataset_bytes).hexdigest()
    if actual_dataset_sha != manifest["dataset_sha256"]:
        raise ValueError("rollout dataset hash does not match manifest")
    examples = load_behavior_examples(DATASET)
    train = [example for example in examples if example.split == "train"]
    validation = [example for example in examples if example.split == "validation"]
    action_size = len(manifest["vocabulary"]["action_space"])

    ngram = NGramBehaviorPolicy(action_size=action_size, order=2)
    ngram.fit(train)
    ngram_metrics = {
        "train": ngram.evaluate(train),
        "validation": ngram.evaluate(validation),
    }

    config = TinyTransformerConfig(
        action_size=action_size,
        max_prefix_length=max(len(example.token_ids) for example in examples),
        d_model=args.d_model,
        seed=args.seed,
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
    model.save(MODEL_OUT)
    reloaded = NumpyMaskedTransformer.load(MODEL_OUT)
    reload_validation = reloaded.evaluate(validation)

    environment_config = AlphaEnvConfig(**manifest["source"]["environment_config"])
    generation = {
        "random": _generation_metrics(
            RandomTokenPolicy(seed=args.seed, stop_probability=environment_config.stop_probability),
            environment_config,
            count=args.generation_samples,
            seed=args.seed,
        ),
        "ngram": _generation_metrics(
            MaskedLogitPolicy(
                ngram.logits,
                seed=args.seed,
                temperature=args.temperature,
                name="ngram",
            ),
            environment_config,
            count=args.generation_samples,
            seed=args.seed,
        ),
        "transformer": _generation_metrics(
            MaskedLogitPolicy(
                reloaded.logits,
                seed=args.seed,
                temperature=args.temperature,
                name="numpy_transformer",
            ),
            environment_config,
            count=args.generation_samples,
            seed=args.seed,
        ),
    }
    transformer_unique_rate = generation["transformer"]["unique_rate"]
    diversity_threshold = 0.25
    reward_gate = {
        "required_unique_rate": diversity_threshold,
        "observed_unique_rate": transformer_unique_rate,
        "passed": transformer_unique_rate >= diversity_threshold,
        "decision": (
            "eligible for equal-budget training-reward evaluation"
            if transformer_unique_rate >= diversity_threshold
            else "deferred: expand training-only rollouts before reward evaluation"
        ),
    }
    equal_budget = None
    if reward_gate["passed"]:
        p10_payload = json.loads(P10.read_text(encoding="utf-8"))
        source_budgets = {
            int(p10_payload["searches"][method]["evaluation_budget"])
            for method in ("random", "evolution")
        }
        if source_budgets != {args.evaluation_budget}:
            raise ValueError(
                "evaluation-budget must match the P10 random/evolution budget "
                f"{sorted(source_budgets)}"
            )
        training_specs = [
            FoldSpec(
                str(fold["fold_id"]),
                date.fromisoformat(fold["start"]),
                date.fromisoformat(fold["end"]),
                "train",
            )
            for fold in p10_payload["walk_forward_folds"]
            if fold["role"] == "train"
        ]
        symbols = p10_payload["config"]["universe"]["symbols"]
        training_datasets = [
            load_fold_dataset(symbols, spec)
            for spec in training_specs
        ]
        evaluator = TrainingEvaluator(training_datasets)
        reward = RobustReward(
            RobustRewardConfig(max_complexity=environment_config.max_complexity)
        )
        correlation_threshold = float(
            p10_payload["config"]["search"]["correlation_threshold"]
        )
        equal_budget = {
            "data_boundary": (
                "T1-T3 TrainingEvaluator only; HOLDOUT branch is never passed to evaluator "
                "or reward"
            ),
            "candidate_budget_per_policy": args.evaluation_budget,
            "random": _source_search_summary(p10_payload, "random"),
            "evolution": _source_search_summary(p10_payload, "evolution"),
            "ngram": _evaluate_policy(
                policy=MaskedLogitPolicy(
                    ngram.logits,
                    seed=args.seed + 1000,
                    temperature=args.temperature,
                    name="ngram_bc",
                ),
                policy_name="ngram_bc",
                environment_config=environment_config,
                evaluator=evaluator,
                reward=reward,
                candidate_budget=args.evaluation_budget,
                correlation_threshold=correlation_threshold,
                seed=args.seed + 1000,
            ),
            "transformer": _evaluate_policy(
                policy=MaskedLogitPolicy(
                    reloaded.logits,
                    seed=args.seed + 2000,
                    temperature=args.temperature,
                    name="transformer_bc",
                ),
                policy_name="transformer_bc",
                environment_config=environment_config,
                evaluator=evaluator,
                reward=reward,
                candidate_budget=args.evaluation_budget,
                correlation_threshold=correlation_threshold,
                seed=args.seed + 2000,
            ),
        }
    report = {
        "schema_version": 1,
        "phase": "P11-B masked behavior cloning v1",
        "config": {
            "model": asdict(config),
            "epochs": args.epochs,
            "batch_size": args.batch_size,
            "early_stopping_patience": args.early_stopping_patience,
            "generation_samples": args.generation_samples,
            "temperature": args.temperature,
            "evaluation_budget": args.evaluation_budget,
            "gpu_required": False,
            "external_ml_dependency": False,
        },
        "data": {
            "dataset": "artifacts/archive/factors/alphagpt_rollouts_v1.jsonl",
            "dataset_sha256": actual_dataset_sha,
            "train_transitions": len(train),
            "validation_transitions": len(validation),
            "boundary": "training rollouts only; P10 HOLDOUT is not read",
        },
        "ngram_baseline": ngram_metrics,
        "transformer_training": training,
        "checkpoint_reload_validation": reload_validation,
        "generation": generation,
        "reward_evaluation_gate": reward_gate,
        "equal_budget_training_reward_comparison": equal_budget,
        "known_limits": [
            "only 201 training transitions are available, so overfitting risk is high",
            "teacher replay does not contain mutation/crossover proposal probabilities",
            "generation metrics measure syntax/diversity, not factor reward or OOS alpha",
        ],
        "next_gate": (
            "expand training-only multi-seed rollouts before equal-budget reward evaluation "
            "or PPO; do not use P10 HOLDOUT for tuning"
        ),
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
    validation = report["transformer_training"]["final"]["validation"]
    print(
        "[alphagpt-bc] complete | "
        f"val_acc={validation['accuracy']:.3f} val_nll={validation['nll']:.3f} "
        f"| {REPORT_OUT}",
        flush=True,
    )


if __name__ == "__main__":
    main()
