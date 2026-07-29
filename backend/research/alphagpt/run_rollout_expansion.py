"""P11-C 前置：在固定训练数据上运行多 seed evolution，扩充离线 rollout。"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from datetime import date

from research.alphagpt.dataset import write_rollout_dataset
from research.alphagpt.environment import AlphaEnvConfig
from research.alphagpt.evolution import FormulaSearch, SearchConfig
from research.alphagpt.reward import RobustReward, RobustRewardConfig
from research.alphagpt.rollouts import (
    RolloutCollection,
    collect_p10_evolution_rollouts,
    replay_teacher_formula,
)
from research.alphagpt.run_alphagpt_v1 import FoldSpec, TrainingEvaluator, load_fold_dataset
from research.paths import FACTOR_ARTIFACTS_DIR, LOGS_DIR, ensure_artifact_dirs

SOURCE = FACTOR_ARTIFACTS_DIR / "alphagpt_v1.json"
DATASET_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_multiseed_v1.jsonl"
MANIFEST_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_multiseed_v1_manifest.json"
CHECKPOINT_DIR = LOGS_DIR / "alphagpt_rollout_expansion"
DEFAULT_SEEDS = (20260725, 20260726, 20260727, 20260728)


def _parse_seeds(value: str) -> tuple[int, ...]:
    seeds = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not seeds or len(set(seeds)) != len(seeds):
        raise argparse.ArgumentTypeError("seeds must be a non-empty unique comma list")
    return seeds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seeds",
        type=_parse_seeds,
        default=DEFAULT_SEEDS,
    )
    parser.add_argument("--candidate-budget", type=int, default=40)
    parser.add_argument("--population-size", type=int, default=10)
    parser.add_argument("--elite-size", type=int, default=4)
    parser.add_argument("--validation-fraction", type=float, default=0.20)
    parser.add_argument("--resume", action="store_true")
    return parser


def run(args: argparse.Namespace) -> dict:
    ensure_artifact_dirs()
    source_bytes = SOURCE.read_bytes()
    payload = json.loads(source_bytes.decode("utf-8"))
    base = collect_p10_evolution_rollouts(payload)
    environment_base = AlphaEnvConfig(**payload["config"]["environment"])
    training_specs = [
        FoldSpec(
            str(fold["fold_id"]),
            date.fromisoformat(fold["start"]),
            date.fromisoformat(fold["end"]),
            "train",
        )
        for fold in payload["walk_forward_folds"]
        if fold["role"] == "train"
    ]
    symbols = list(payload["config"]["universe"]["symbols"])
    training_datasets = [load_fold_dataset(symbols, spec) for spec in training_specs]
    evaluator = TrainingEvaluator(training_datasets)
    reward = RobustReward(
        RobustRewardConfig(max_complexity=environment_base.max_complexity)
    )
    correlation_threshold = float(
        payload["config"]["search"]["correlation_threshold"]
    )

    episodes = list(base.episodes)
    failures = list(base.failures)
    seen = {episode.formula_hash for episode in episodes}
    per_seed: list[dict] = []
    for seed in args.seeds:
        environment_config = replace(environment_base, seed=seed)
        search_config = SearchConfig(
            candidate_budget=args.candidate_budget,
            population_size=args.population_size,
            elite_size=args.elite_size,
            crossover_probability=float(
                payload["config"]["search"]["crossover_probability"]
            ),
            correlation_threshold=correlation_threshold,
            final_candidate_count=int(
                payload["config"]["search"]["final_candidate_count"]
            ),
            seed=seed,
            data_fingerprint=base.data_fingerprint,
        )
        search = FormulaSearch(
            method="evolution",
            evaluator=evaluator,
            environment_config=environment_config,
            search_config=search_config,
            reward=reward,
            checkpoint_path=CHECKPOINT_DIR / f"seed_{seed}.checkpoint.json",
        ).run(resume=args.resume)
        added = 0
        cross_seed_duplicates = 0
        for index, candidate in enumerate(search.pool.ranked_candidates()):
            if candidate.formula_hash in seen:
                cross_seed_duplicates += 1
                failures.append(
                    {
                        "reason": "cross_seed_duplicate",
                        "seed": seed,
                        "candidate_id": candidate.candidate_id,
                        "formula": candidate.formula,
                        "formula_hash": candidate.formula_hash,
                    }
                )
                continue
            try:
                episode = replay_teacher_formula(
                    tokens=candidate.tokens,
                    environment_config=environment_config,
                    episode_id=f"p11c_s{seed}_{candidate.candidate_id}",
                    seed=seed + index,
                    policy_name="multiseed_evolution_teacher",
                )
                episodes.append(
                    replace(
                        episode,
                        evaluation_status="training_evaluated",
                        final_reward=float(candidate.reward["total"]),
                        reward_breakdown=dict(candidate.reward),
                        training_fold_metrics=tuple(candidate.fold_metrics),
                        provenance={
                            "source_phase": "P11-C rollout expansion",
                            "source_search_method": "evolution",
                            "source_seed": seed,
                            "source_candidate_id": candidate.candidate_id,
                            "generation_method": candidate.generation_method,
                            "parent_formulas": list(candidate.parent_formulas),
                            "selection": "accepted per-seed evolution pool candidate",
                        },
                    )
                )
                seen.add(candidate.formula_hash)
                added += 1
            except Exception as exc:
                failures.append(
                    {
                        "reason": "teacher_replay_failure",
                        "seed": seed,
                        "candidate_id": candidate.candidate_id,
                        "formula": candidate.formula,
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
        failures.extend(
            {
                "reason": failure.reason,
                "seed": seed,
                "formula": failure.formula,
                "formula_hash": failure.formula_hash,
                "generation_method": failure.generation_method,
                "details": failure.details,
            }
            for failure in search.pool.failures
        )
        per_seed.append(
            {
                "seed": seed,
                "evaluation_budget": search.evaluation_budget,
                "evaluations_used": search.evaluations_used,
                "n_accepted_within_seed": len(search.pool.accepted_candidates()),
                "n_added_after_cross_seed_dedupe": added,
                "cross_seed_duplicates": cross_seed_duplicates,
            }
        )

    collection = RolloutCollection(
        episodes=tuple(episodes),
        failures=tuple(failures),
        source_seed=base.source_seed,
        data_fingerprint=base.data_fingerprint,
        environment_config=asdict(environment_base),
        source_metadata={
            "artifact": "artifacts/archive/factors/alphagpt_v1.json + multiseed training searches",
            "search_method": "evolution multi-seed",
            "metric_scope": "T1-T3 training folds only; HOLDOUT is never loaded",
            "expansion_seeds": list(args.seeds),
            "candidate_budget_per_seed": args.candidate_budget,
            "per_seed": per_seed,
        },
    )
    return write_rollout_dataset(
        collection,
        dataset_path=DATASET_OUT,
        manifest_path=MANIFEST_OUT,
        source_artifact_sha256=hashlib.sha256(source_bytes).hexdigest(),
        validation_fraction=args.validation_fraction,
    )


def main() -> None:
    args = build_parser().parse_args()
    manifest = run(args)
    counts = manifest["counts"]
    print(
        "[alphagpt-rollout-expansion] complete | "
        f"episodes={counts['episodes']} transitions={counts['transitions']} "
        f"failures={counts['failures']} | {DATASET_OUT}",
        flush=True,
    )


if __name__ == "__main__":
    main()
