"""P11-E：训练 seed 留一 CV 的 pairwise/listwise 公式排序模型。"""

from __future__ import annotations

import argparse
import hashlib
import json

import numpy as np

from research.alphagpt.environment import AlphaEnv, AlphaEnvConfig
from research.alphagpt.rank_model import (
    FormulaRanker,
    pairwise_accuracy,
    select_ranker_by_group_cv,
)
from research.alphagpt.reward_labels import load_reward_labels, write_json_atomic
from research.alphagpt.reward_model import (
    FormulaFeatureConfig,
    FormulaFeaturizer,
    FormulaRewardExample,
    calibration_bins,
    regression_metrics,
    spearman_correlation,
    top_k_metrics,
)
from research.paths import FACTOR_ARTIFACTS_DIR, ensure_artifact_dirs

LABELS = FACTOR_ARTIFACTS_DIR / "alphagpt_reward_labels_v2.json"
MODEL_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_rank_model_v2.npz"
REPORT_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_rank_model_v2.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alphas", default="0.1,1,10,100")
    parser.add_argument("--min-reward-gap", type=float, default=0.10)
    parser.add_argument("--max-pairs-per-seed", type=int, default=128)
    parser.add_argument("--top-fraction", type=float, default=0.20)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260827)
    return parser


def _bootstrap_spearman(
    actual: np.ndarray,
    predicted: np.ndarray,
    *,
    samples: int,
    seed: int,
) -> dict[str, float | int]:
    if samples <= 0:
        raise ValueError("bootstrap samples must be > 0")
    rng = np.random.default_rng(seed)
    values = []
    for _ in range(samples):
        indices = rng.integers(0, len(actual), len(actual))
        values.append(spearman_correlation(actual[indices], predicted[indices]))
    return {
        "samples": samples,
        "p05": float(np.quantile(values, 0.05)),
        "median": float(np.median(values)),
        "p95": float(np.quantile(values, 0.95)),
    }


def run(args: argparse.Namespace) -> dict:
    ensure_artifact_dirs()
    label_bytes = LABELS.read_bytes()
    payload = json.loads(label_bytes)
    labels = load_reward_labels(LABELS)
    train = [label for label in labels if label.split == "train"]
    validation = [label for label in labels if label.split == "validation"]
    environment_config = AlphaEnvConfig(**payload["config"]["environment"])
    action_space = AlphaEnv(environment_config).action_space
    feature_config = FormulaFeatureConfig(
        action_space=action_space,
        max_formula_length=environment_config.max_formula_length,
        max_complexity=environment_config.max_complexity,
    )
    featurizer = FormulaFeaturizer(feature_config)

    def as_examples(items):
        return [
            FormulaRewardExample(
                formula_hash=item.formula_hash,
                tokens=item.tokens,
                reward=item.intrinsic_reward,
                split=item.split,
            )
            for item in items
        ]

    train_features = featurizer.transform(as_examples(train))
    validation_features = featurizer.transform(as_examples(validation))
    train_rewards = np.asarray(
        [label.intrinsic_reward for label in train],
        dtype=float,
    )
    validation_rewards = np.asarray(
        [label.intrinsic_reward for label in validation],
        dtype=float,
    )
    train_groups = np.asarray([label.data_seed for label in train], dtype=int)
    alphas = tuple(float(value) for value in args.alphas.split(",") if value.strip())
    cv = select_ranker_by_group_cv(
        train_features,
        train_rewards,
        train_groups,
        objectives=("pairwise", "listwise"),
        alphas=alphas,
        min_reward_gap=args.min_reward_gap,
        max_pairs_per_group=args.max_pairs_per_seed,
    )
    model = FormulaRanker(
        objective=cv["selected_objective"],
        alpha=float(cv["selected_alpha"]),
        min_reward_gap=args.min_reward_gap,
        max_pairs_per_group=args.max_pairs_per_seed,
    ).fit(train_features, train_rewards, train_groups)
    model.save(MODEL_OUT, feature_config=feature_config)
    restored, restored_config = FormulaRanker.load(MODEL_OUT)
    if restored_config != feature_config:
        raise AssertionError("rank model feature config changed after reload")
    train_predictions = restored.predict(train_features)
    validation_predictions = restored.predict(validation_features)
    validation_metrics = regression_metrics(
        validation_rewards,
        validation_predictions,
    )
    validation_metrics["pairwise_accuracy"] = pairwise_accuracy(
        validation_rewards,
        validation_predictions,
    )
    top_k = top_k_metrics(
        validation_rewards,
        validation_predictions,
        fraction=args.top_fraction,
    )
    bootstrap = _bootstrap_spearman(
        validation_rewards,
        validation_predictions,
        samples=args.bootstrap_samples,
        seed=args.seed,
    )
    checks = {
        "validation_examples_at_least_60": len(validation) >= 60,
        "validation_spearman_at_least_0_20": validation_metrics["spearman"] >= 0.20,
        "validation_pairwise_accuracy_above_0_55": (
            validation_metrics["pairwise_accuracy"] > 0.55
        ),
        "top_k_absolute_lift_positive": top_k["absolute_lift"] > 0,
        "top_k_actual_mean_positive": top_k["selected_actual_mean"] > 0,
        "bootstrap_p05_above_minus_0_10": bootstrap["p05"] > -0.10,
    }
    gate_passed = all(checks.values())
    report = {
        "schema_version": 2,
        "phase": "P11-E pairwise/listwise formula rank model v2",
        "config": {
            "objectives": ["pairwise", "listwise"],
            "alphas": list(alphas),
            "min_reward_gap": args.min_reward_gap,
            "max_pairs_per_seed": args.max_pairs_per_seed,
            "top_fraction": args.top_fraction,
            "bootstrap_samples": args.bootstrap_samples,
            "seed": args.seed,
            "feature_count": len(featurizer.feature_names),
            "gpu_required": False,
        },
        "data": {
            "labels": LABELS.name,
            "labels_sha256": hashlib.sha256(label_bytes).hexdigest(),
            "train_formulas": len(train),
            "validation_formulas": len(validation),
            "train_seeds": sorted(set(train_groups.tolist())),
            "validation_seeds": sorted(
                {label.data_seed for label in validation}
            ),
            "model_target": "intrinsic_reward",
            "boundary": "T1-T3 only; HOLDOUT excluded",
        },
        "training_cv": cv,
        "selected_model": {
            "objective": model.objective,
            "alpha": model.alpha,
            "training_pairs": model.training_pairs,
        },
        "train_metrics": {
            **regression_metrics(train_rewards, train_predictions),
            "pairwise_accuracy": pairwise_accuracy(
                train_rewards,
                train_predictions,
            ),
        },
        "validation_metrics": validation_metrics,
        "validation_spearman_bootstrap_90pct": bootstrap,
        "validation_top_k": top_k,
        "validation_calibration": calibration_bins(
            validation_rewards,
            validation_predictions,
        ),
        "gate": {
            "checks": checks,
            "passed": gate_passed,
            "decision": (
                "eligible for one prospective unseen-seed reranker test"
                if gate_passed
                else "do not run or integrate the v2 reranker"
            ),
        },
        "checkpoint": {
            "file": MODEL_OUT.name,
            "sha256": hashlib.sha256(MODEL_OUT.read_bytes()).hexdigest(),
            "reload_predictions_identical": bool(
                np.array_equal(
                    model.predict(validation_features),
                    validation_predictions,
                )
            ),
        },
        "leakage_controls": {
            "hyperparameter_selection": "leave-one-complete-train-seed-out CV",
            "validation_usage": "one locked gate after model selection and fit",
            "target": "intrinsic T1-T3 reward without unobservable pool correlation",
            "holdout_usage": "none",
        },
        "known_limits": [
            "random legal formulas remain a small synthetic search distribution",
            "validation covers two data seeds only",
            "intrinsic rewards are training-fold labels, not OOS evidence",
            "prospective reranking is forbidden unless every gate check passes",
        ],
    }
    write_json_atomic(REPORT_OUT, report)
    return report


def main() -> None:
    args = build_parser().parse_args()
    report = run(args)
    print(
        "[alphagpt-rank-model-v2] complete | "
        f"objective={report['selected_model']['objective']} "
        f"gate={'PASS' if report['gate']['passed'] else 'FAIL'} "
        f"spearman={report['validation_metrics']['spearman']:+.3f} "
        f"lift={report['validation_top_k']['absolute_lift']:+.3f} "
        f"| {REPORT_OUT}",
        flush=True,
    )


if __name__ == "__main__":
    main()
