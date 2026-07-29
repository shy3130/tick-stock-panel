"""确定性 AlphaGPT rollout JSONL 数据集格式。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from research.alphagpt.environment import AlphaEnv
from research.alphagpt.rollouts import RolloutCollection, RolloutEpisode

DATASET_SCHEMA_VERSION = 1


def deterministic_split(formula_digest: str, *, validation_fraction: float) -> str:
    if not 0.0 <= validation_fraction < 1.0:
        raise ValueError("validation_fraction must be within [0, 1)")
    bucket = int(formula_digest[:16], 16) / float(16**16)
    return "validation" if bucket < validation_fraction else "train"


def episode_transitions(
    episode: RolloutEpisode,
    *,
    split: str,
) -> Iterable[dict[str, Any]]:
    if episode.final_reward is None:
        raise ValueError(f"episode {episode.episode_id} has no final training reward")
    for step in episode.steps:
        yield {
            "schema_version": DATASET_SCHEMA_VERSION,
            "dataset_role": "training_policy_rollout",
            "split": split,
            "episode_id": episode.episode_id,
            "policy_name": episode.policy_name,
            "seed": episode.seed,
            **step.to_dict(),
            "outcome": {
                "formula": " ".join(episode.formula_tokens),
                "formula_hash": episode.formula_hash,
                "evaluation_status": episode.evaluation_status,
                "final_training_reward": episode.final_reward,
                "reward_breakdown": episode.reward_breakdown,
            },
            "provenance": episode.provenance,
        }


def write_rollout_dataset(
    collection: RolloutCollection,
    *,
    dataset_path: Path,
    manifest_path: Path,
    source_artifact_sha256: str,
    validation_fraction: float = 0.20,
) -> dict[str, Any]:
    """原子写 JSONL 和 manifest；相同输入保证字节级一致。"""

    if not collection.episodes:
        raise ValueError("rollout collection is empty")
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    episode_entries: list[dict[str, Any]] = []
    split_episode_counts = {"train": 0, "validation": 0}
    split_transition_counts = {"train": 0, "validation": 0}
    for episode in collection.episodes:
        split = deterministic_split(
            episode.formula_hash,
            validation_fraction=validation_fraction,
        )
        split_episode_counts[split] += 1
        episode_entries.append(episode.to_manifest_entry(split=split))
        for transition in episode_transitions(episode, split=split):
            lines.append(
                json.dumps(
                    transition,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
            split_transition_counts[split] += 1
    content = "\n".join(lines) + "\n"
    dataset_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()

    temporary_dataset = dataset_path.with_suffix(dataset_path.suffix + ".tmp")
    temporary_dataset.write_bytes(content.encode("utf-8"))
    temporary_dataset.replace(dataset_path)

    action_space = list(AlphaEnv().action_space)
    source = {
        "artifact": "artifacts/archive/factors/alphagpt_v1.json",
        "artifact_sha256": source_artifact_sha256,
        "search_method": "evolution",
        "candidate_scope": "accepted pool candidates only",
        "metric_scope": "P10 training folds only; final candidate reports are not read",
        "source_seed": collection.source_seed,
        "data_fingerprint": collection.data_fingerprint,
        "environment_config": collection.environment_config,
    }
    source.update(collection.source_metadata)
    manifest = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "phase": "P11-A AlphaGPT policy rollouts",
        "dataset_role": "training_only",
        "source": source,
        "vocabulary": {
            "action_space": action_space,
            "action_to_id": {
                action: index for index, action in enumerate(action_space)
            },
        },
        "split_rule": {
            "method": "first 64 bits of normalized formula SHA-256",
            "validation_fraction": validation_fraction,
        },
        "counts": {
            "episodes": len(collection.episodes),
            "transitions": len(lines),
            "episodes_by_split": split_episode_counts,
            "transitions_by_split": split_transition_counts,
            "failures": len(collection.failures),
        },
        "dataset_sha256": dataset_sha256,
        "episodes": episode_entries,
        "failures": list(collection.failures),
        "known_limits": [
            "teacher replay reconstructs token decisions from final evolution formulas",
            "it does not reconstruct mutation/crossover proposal probabilities",
            "the dataset is for policy pretraining, not OOS performance claims",
        ],
    }
    temporary_manifest = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary_manifest.replace(manifest_path)
    return manifest


def read_transitions(path: Path) -> list[dict[str, Any]]:
    transitions: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("schema_version") != DATASET_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema at line {line_number}")
        transitions.append(item)
    return transitions
