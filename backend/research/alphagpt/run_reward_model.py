"""P11-D：训练公式级 ridge reward model，验证 rank correlation 与 top-k lift。"""

from __future__ import annotations

import argparse
import hashlib
import json

import numpy as np

from research.alphagpt.reward_model import (
    FormulaFeatureConfig,
    FormulaFeaturizer,
    RidgeRewardModel,
    calibration_bins,
    load_formula_reward_examples,
    regression_metrics,
    select_alpha_by_training_cv,
    top_k_metrics,
)
from research.paths import FACTOR_ARTIFACTS_DIR, ensure_artifact_dirs

MANIFEST = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_multiseed_v1_manifest.json"
DATASET = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_multiseed_v1.jsonl"
MODEL_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_reward_model_v1.npz"
REPORT_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_reward_model_v1.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alphas", default="0.1,1,10,100")
    parser.add_argument("--cv-folds", type=int, default=4)
    parser.add_argument("--top-fraction", type=float, default=0.20)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260810)
    return parser


def _bootstrap_spearman(actual, predicted, *, samples: int, seed: int) -> dict:
    from research.alphagpt.reward_model import spearman_correlation

    if samples <= 0:
        raise ValueError("bootstrap samples must be > 0")
    rng = np.random.default_rng(seed)
    y = np.asarray(actual, dtype=float)
    p = np.asarray(predicted, dtype=float)
    values = []
    for _ in range(samples):
        indices = rng.integers(0, len(y), len(y))
        values.append(spearman_correlation(y[indices], p[indices]))
    return {
        "samples": samples,
        "p05": float(np.quantile(values, 0.05)),
        "median": float(np.median(values)),
        "p95": float(np.quantile(values, 0.95)),
    }


def run(args: argparse.Namespace) -> dict:
    ensure_artifact_dirs()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    dataset_sha = hashlib.sha256(DATASET.read_bytes()).hexdigest()
    if dataset_sha != manifest["dataset_sha256"]:
        raise ValueError("expanded rollout hash does not match manifest")
    examples = load_formula_reward_examples(MANIFEST)
    train = [example for example in examples if example.split == "train"]
    validation = [example for example in examples if example.split == "validation"]
    if not train or not validation:
        raise ValueError("both formula-level train and validation splits are required")
    environment = manifest["source"]["environment_config"]
    feature_config = FormulaFeatureConfig(
        action_space=tuple(manifest["vocabulary"]["action_space"]),
        max_formula_length=int(environment["max_formula_length"]),
        max_complexity=int(environment["max_complexity"]),
    )
    featurizer = FormulaFeaturizer(feature_config)
    train_features = featurizer.transform(train)
    validation_features = featurizer.transform(validation)
    train_targets = np.asarray([example.reward for example in train], dtype=float)
    validation_targets = np.asarray(
        [example.reward for example in validation],
        dtype=float,
    )
    alphas = tuple(float(value) for value in args.alphas.split(","))
    cv = select_alpha_by_training_cv(
        train,
        train_features,
        alphas=alphas,
        n_folds=args.cv_folds,
    )
    model = RidgeRewardModel(alpha=float(cv["selected_alpha"])).fit(
        train_features,
        train_targets,
    )
    model.save(MODEL_OUT, feature_config=feature_config)
    restored, restored_config = RidgeRewardModel.load(MODEL_OUT)
    if restored_config != feature_config:
        raise AssertionError("reward model feature config changed after reload")
    train_predictions = restored.predict(train_features)
    validation_predictions = restored.predict(validation_features)
    train_metrics = regression_metrics(train_targets, train_predictions)
    validation_metrics = regression_metrics(
        validation_targets,
        validation_predictions,
    )
    top_k = top_k_metrics(
        validation_targets,
        validation_predictions,
        fraction=args.top_fraction,
    )
    bootstrap = _bootstrap_spearman(
        validation_targets,
        validation_predictions,
        samples=args.bootstrap_samples,
        seed=args.seed,
    )
    gate_checks = {
        "validation_examples_at_least_15": len(validation) >= 15,
        "validation_spearman_at_least_0_20": validation_metrics["spearman"] >= 0.20,
        "top_k_absolute_lift_positive": top_k["absolute_lift"] > 0,
        "top_k_actual_mean_positive": top_k["selected_actual_mean"] > 0,
        "bootstrap_p05_above_minus_0_10": bootstrap["p05"] > -0.10,
    }
    gate_passed = all(gate_checks.values())
    report = {
        "schema_version": 1,
        "phase": "P11-D formula reward model v1",
        "config": {
            "alphas": list(alphas),
            "cv_folds": args.cv_folds,
            "top_fraction": args.top_fraction,
            "bootstrap_samples": args.bootstrap_samples,
            "seed": args.seed,
            "model": "standardized dual ridge on fixed formula structure features",
            "feature_count": len(featurizer.feature_names),
            "gpu_required": False,
        },
        "data": {
            "manifest": "artifacts/archive/factors/alphagpt_rollouts_multiseed_v1_manifest.json",
            "dataset_sha256": dataset_sha,
            "train_formulas": len(train),
            "validation_formulas": len(validation),
            "boundary": "formula and RobustReward labels from T1-T3 only; HOLDOUT excluded",
        },
        "leakage_controls": {
            "model_fit_inputs": "formula tokens and train-split final training rewards only",
            "alpha_selection": "formula-hash CV within train split only",
            "validation_usage": "one locked rank/top-k/calibration gate after fitting",
            "holdout_usage": "none; HOLDOUT is neither loaded nor represented in labels",
            "formula_hashes_verified_and_unique": True,
            "market_features_used_by_model": False,
        },
        "training_cv": cv,
        "train_metrics": train_metrics,
        "validation_metrics": validation_metrics,
        "validation_spearman_bootstrap_90pct": bootstrap,
        "validation_top_k": top_k,
        "validation_calibration": calibration_bins(
            validation_targets,
            validation_predictions,
        ),
        "gate": {
            "checks": gate_checks,
            "passed": gate_passed,
            "decision": (
                "eligible for prospective training-only reranker test"
                if gate_passed
                else "reward model is not reliable enough for candidate reranking"
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
        "known_limits": [
            "only 19 validation formulas are available",
            "all labels are training-fold rewards and are not OOS evidence",
            "558 fixed features on 117 train formulas can interpolate; OOF and prospective gates are authoritative",
            "calibration is biased at the low predicted-reward tail",
            "no prospective candidate reranking is run unless the gate passes",
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
        "[alphagpt-reward-model] complete | "
        f"gate={'PASS' if report['gate']['passed'] else 'FAIL'} "
        f"spearman={report['validation_metrics']['spearman']:+.3f} "
        f"lift={report['validation_top_k']['absolute_lift']:+.3f} "
        f"| {REPORT_OUT}",
        flush=True,
    )


if __name__ == "__main__":
    main()
