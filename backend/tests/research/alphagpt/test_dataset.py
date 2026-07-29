from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from research.alphagpt.dataset import read_transitions, write_rollout_dataset
from research.alphagpt.rollouts import collect_p10_evolution_rollouts
from tests.research.alphagpt.test_rollouts import p10_payload


def test_jsonl_dataset_is_deterministic_and_contains_training_reward(tmp_path) -> None:
    collection = collect_p10_evolution_rollouts(p10_payload())
    left_data = tmp_path / "left.jsonl"
    left_manifest = tmp_path / "left.manifest.json"
    right_data = tmp_path / "right.jsonl"
    right_manifest = tmp_path / "right.manifest.json"

    left = write_rollout_dataset(
        collection,
        dataset_path=left_data,
        manifest_path=left_manifest,
        source_artifact_sha256="abc123",
    )
    right = write_rollout_dataset(
        collection,
        dataset_path=right_data,
        manifest_path=right_manifest,
        source_artifact_sha256="abc123",
    )

    assert left_data.read_bytes() == right_data.read_bytes()
    assert left == right
    assert left["dataset_sha256"] == hashlib.sha256(left_data.read_bytes()).hexdigest()
    transitions = read_transitions(left_data)
    assert len(transitions) == left["counts"]["transitions"]
    assert all(item["dataset_role"] == "training_policy_rollout" for item in transitions)
    assert all(item["outcome"]["final_training_reward"] in {1.0, 2.0} for item in transitions)
    assert all(
        len(item["observation"]["action_mask"])
        == len(left["vocabulary"]["action_space"])
        for item in transitions
    )


def test_dataset_does_not_copy_holdout_reports_from_source(tmp_path) -> None:
    payload = p10_payload()
    collection = collect_p10_evolution_rollouts(payload)
    dataset_path = tmp_path / "rollouts.jsonl"
    manifest_path = tmp_path / "manifest.json"
    write_rollout_dataset(
        collection,
        dataset_path=dataset_path,
        manifest_path=manifest_path,
        source_artifact_sha256="source",
    )

    combined = dataset_path.read_text(encoding="utf-8") + manifest_path.read_text(
        encoding="utf-8"
    )
    assert "999999" not in combined
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["source"]["metric_scope"].startswith("P10 training folds only")
    assert all(
        fold["dataset_role"] == "train"
        for episode in manifest["episodes"]
        for fold in episode["training_fold_metrics"]
    )


def test_manifest_carries_multiseed_source_metadata(tmp_path) -> None:
    collection = replace(
        collect_p10_evolution_rollouts(p10_payload()),
        source_metadata={
            "search_method": "evolution multi-seed",
            "expansion_seeds": [11, 12],
        },
    )
    manifest = write_rollout_dataset(
        collection,
        dataset_path=tmp_path / "rollouts.jsonl",
        manifest_path=tmp_path / "manifest.json",
        source_artifact_sha256="source",
    )
    assert manifest["source"]["search_method"] == "evolution multi-seed"
    assert manifest["source"]["expansion_seeds"] == [11, 12]
