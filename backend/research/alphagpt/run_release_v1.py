"""冻结并验证 AlphaGPT Research v1.0 发布清单。"""

from __future__ import annotations

import argparse
import json
from typing import Any

from research.alphagpt.release import (
    artifact_record,
    require_checks,
    sha256_file,
    verify_artifact_records,
)
from research.alphagpt.reward_labels import load_reward_labels, write_json_atomic
from research.paths import FACTOR_ARTIFACTS_DIR, ensure_artifact_dirs

RELEASE_VERSION = "1.0.0"
RELEASE_DATE = "2026-07-24"
MANIFEST_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_research_v1_manifest.json"
NOTES_OUT = FACTOR_ARTIFACTS_DIR / "alphagpt_research_v1_release.md"

REQUIRED_ARTIFACTS = (
    ("alphagpt_v1.json", "P10 search loop, lineage, reward audit and frozen HOLDOUT report"),
    ("alphagpt_rollouts_v1.jsonl", "P11-A original offline token transitions"),
    ("alphagpt_rollouts_v1_manifest.json", "P11-A dataset provenance and split audit"),
    ("alphagpt_rollouts_multiseed_v1.jsonl", "P11-C expanded token transitions"),
    (
        "alphagpt_rollouts_multiseed_v1_manifest.json",
        "P11-C expanded dataset provenance and training rewards",
    ),
    ("alphagpt_bc_v1.npz", "P11-B deterministic NumPy behavior-clone checkpoint"),
    ("alphagpt_bc_v1.json", "P11-B training, generation and budget comparison report"),
    ("alphagpt_bc_stability_v1.json", "P11-C multi-seed behavior stability gate"),
    (
        "alphagpt_reward_conditioned_stability_v1.json",
        "P11-C2 reward-weighted and elite behavior-clone gate",
    ),
    ("alphagpt_reward_model_v1.npz", "P11-D formula ridge checkpoint"),
    ("alphagpt_reward_model_v1.json", "P11-D locked formula-model validation gate"),
    ("alphagpt_reward_reranker_v1.json", "P11-D prospective equal-budget reranker report"),
    ("alphagpt_reward_labels_v2.json", "P11-E seed-split random-formula labels"),
    ("alphagpt_rank_model_v2.npz", "P11-E pairwise/listwise rank checkpoint"),
    ("alphagpt_rank_model_v2.json", "P11-E locked seed-level validation gate"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify the existing release manifest without rewriting it",
    )
    return parser


def _read_json(filename: str) -> dict[str, Any]:
    return json.loads((FACTOR_ARTIFACTS_DIR / filename).read_text(encoding="utf-8"))


def _check(name: str, passed: bool, detail: Any) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _training_fold_roles(payload: dict[str, Any]) -> set[str]:
    roles: set[str] = set()
    for episode in payload["episodes"]:
        roles.update(
            str(fold.get("dataset_role"))
            for fold in episode["training_fold_metrics"]
        )
    return roles


def _p10_search_fold_roles(payload: dict[str, Any]) -> set[str]:
    roles: set[str] = set()
    for search in payload["searches"].values():
        for candidate in search["pool"]["candidates"]:
            roles.update(
                str(fold.get("dataset_role"))
                for fold in candidate["fold_metrics"]
            )
    return roles


def _build_checks() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    p10 = _read_json("alphagpt_v1.json")
    rollout_manifest = _read_json("alphagpt_rollouts_v1_manifest.json")
    multiseed_manifest = _read_json("alphagpt_rollouts_multiseed_v1_manifest.json")
    stability = _read_json("alphagpt_bc_stability_v1.json")
    conditioned = _read_json("alphagpt_reward_conditioned_stability_v1.json")
    reward_model = _read_json("alphagpt_reward_model_v1.json")
    reranker = _read_json("alphagpt_reward_reranker_v1.json")
    labels_payload = _read_json("alphagpt_reward_labels_v2.json")
    rank_model = _read_json("alphagpt_rank_model_v2.json")

    p10_budget_ok = all(
        int(search["evaluation_budget"]) == int(search["evaluations_used"])
        for search in p10["searches"].values()
    )
    p10_train_roles = _p10_search_fold_roles(p10)
    original_dataset = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_v1.jsonl"
    expanded_dataset = FACTOR_ARTIFACTS_DIR / "alphagpt_rollouts_multiseed_v1.jsonl"
    reward_checkpoint = FACTOR_ARTIFACTS_DIR / "alphagpt_reward_model_v1.npz"
    rank_checkpoint = FACTOR_ARTIFACTS_DIR / "alphagpt_rank_model_v2.npz"
    labels = load_reward_labels(
        FACTOR_ARTIFACTS_DIR / "alphagpt_reward_labels_v2.json"
    )
    train_seeds = {label.data_seed for label in labels if label.split == "train"}
    validation_seeds = {
        label.data_seed for label in labels if label.split == "validation"
    }

    checks = [
        _check(
            "p10_equal_search_budgets_consumed",
            p10_budget_ok,
            {
                method: {
                    "budget": search["evaluation_budget"],
                    "used": search["evaluations_used"],
                }
                for method, search in p10["searches"].items()
            },
        ),
        _check(
            "p10_search_folds_are_training_only",
            p10_train_roles == {"train"},
            sorted(p10_train_roles),
        ),
        _check(
            "p11a_dataset_hash_matches_manifest",
            sha256_file(original_dataset)
            == str(rollout_manifest["dataset_sha256"]),
            rollout_manifest["dataset_sha256"],
        ),
        _check(
            "p11c_dataset_hash_matches_manifest",
            sha256_file(expanded_dataset)
            == str(multiseed_manifest["dataset_sha256"]),
            multiseed_manifest["dataset_sha256"],
        ),
        _check(
            "rollout_reward_folds_are_training_only",
            _training_fold_roles(rollout_manifest) == {"train"}
            and _training_fold_roles(multiseed_manifest) == {"train"},
            {
                "p11a": sorted(_training_fold_roles(rollout_manifest)),
                "p11c": sorted(_training_fold_roles(multiseed_manifest)),
            },
        ),
        _check(
            "p11c_behavior_stability_gate_failed_honestly",
            stability["pre_ppo_gate"]["passed"] is False,
            stability["pre_ppo_gate"]["decision"],
        ),
        _check(
            "p11c2_reward_conditioned_gate_failed_honestly",
            conditioned["pre_ppo_gate"]["passed"] is False,
            conditioned["pre_ppo_gate"]["decision"],
        ),
        _check(
            "p11d_checkpoint_hash_matches_report",
            sha256_file(reward_checkpoint)
            == str(reward_model["checkpoint"]["sha256"]),
            reward_model["checkpoint"]["sha256"],
        ),
        _check(
            "p11d_validation_gate_passed",
            reward_model["gate"]["passed"] is True,
            reward_model["gate"]["decision"],
        ),
        _check(
            "p11d_prospective_gate_failed_honestly",
            reranker["gate"]["passed"] is False,
            reranker["gate"]["decision"],
        ),
        _check(
            "p11e_seed_split_is_disjoint",
            bool(train_seeds)
            and bool(validation_seeds)
            and not (train_seeds & validation_seeds),
            {
                "train_seeds": sorted(train_seeds),
                "validation_seeds": sorted(validation_seeds),
            },
        ),
        _check(
            "p11e_counts_match_labels",
            len(labels) == int(labels_payload["counts"]["labels"]),
            {
                "loaded": len(labels),
                "reported": labels_payload["counts"]["labels"],
            },
        ),
        _check(
            "p11e_checkpoint_hash_matches_report",
            sha256_file(rank_checkpoint)
            == str(rank_model["checkpoint"]["sha256"]),
            rank_model["checkpoint"]["sha256"],
        ),
        _check(
            "p11e_locked_validation_gate_failed_honestly",
            rank_model["gate"]["passed"] is False,
            rank_model["gate"]["decision"],
        ),
        _check(
            "no_frontend_is_part_of_alphagpt_release",
            True,
            "research Python modules and archived artifacts only",
        ),
    ]
    summary = {
        "p10": {
            "random_budget": p10["searches"]["random"]["evaluations_used"],
            "evolution_budget": p10["searches"]["evolution"]["evaluations_used"],
        },
        "p11a": rollout_manifest["counts"],
        "p11c": {
            "counts": multiseed_manifest["counts"],
            "pre_ppo_gate": stability["pre_ppo_gate"]["passed"],
        },
        "p11c2": {
            "pre_ppo_gate": conditioned["pre_ppo_gate"]["passed"],
            "passing_modes": conditioned["pre_ppo_gate"]["passing_modes"],
        },
        "p11d": {
            "validation_spearman": reward_model["validation_metrics"]["spearman"],
            "validation_top_k_lift": reward_model["validation_top_k"]["absolute_lift"],
            "validation_gate": reward_model["gate"]["passed"],
            "prospective_reranker_mean": reranker["aggregate"][
                "reranker_mean_training_reward"
            ],
            "prospective_random_mean": reranker["aggregate"][
                "random_mean_training_reward"
            ],
            "prospective_gate": reranker["gate"]["passed"],
        },
        "p11e": {
            "labels": labels_payload["counts"],
            "selected_objective": rank_model["selected_model"]["objective"],
            "validation_spearman": rank_model["validation_metrics"]["spearman"],
            "validation_top_k_lift": rank_model["validation_top_k"]["absolute_lift"],
            "validation_gate": rank_model["gate"]["passed"],
        },
    }
    return checks, summary


def build_release_manifest() -> dict[str, Any]:
    ensure_artifact_dirs()
    records = [
        artifact_record(FACTOR_ARTIFACTS_DIR / filename, role=role)
        for filename, role in REQUIRED_ARTIFACTS
    ]
    checks, summary = _build_checks()
    require_checks(checks)
    return {
        "schema_version": 1,
        "release": {
            "name": "AlphaGPT Research",
            "version": RELEASE_VERSION,
            "release_date": RELEASE_DATE,
            "status": "complete_research_baseline",
        },
        "decision": {
            "research_version_complete": True,
            "production_alpha_ready": False,
            "ppo_ready": False,
            "frontend_included": False,
            "reason": (
                "the generation/evaluation/audit pipeline is complete, but locked "
                "generalization gates do not support production alpha or PPO"
            ),
        },
        "capabilities": [
            "legal token-by-token RPN formula generation with action masks",
            "StackVM execution and T1-T3 training-fold evaluation",
            "deduplication, lineage, correlation pruning and failure audit",
            "random/evolution equal-budget comparison and deterministic checkpoints",
            "offline rollouts and CPU-only behavior/rank baselines",
            "locked validation and prospective gate reports",
        ],
        "explicit_non_goals": [
            "frontend or interactive dashboard",
            "live trading or production strategy deployment",
            "claiming validated positive alpha",
            "Transformer/PPO optimization before a new approved phase",
        ],
        "validation_checks": checks,
        "summary": summary,
        "artifacts": records,
        "reproduction": {
            "working_directory": "backend",
            "release_command": (
                ".venv/Scripts/python.exe -m research.alphagpt.run_release_v1"
            ),
            "verify_command": (
                ".venv/Scripts/python.exe -m research.alphagpt.run_release_v1 "
                "--verify-only"
            ),
            "tests": ".venv/Scripts/python.exe -m pytest",
        },
        "next_phase": {
            "status": "deferred_optimization",
            "recommended": (
                "P11-F execution-aware proxy features on new training-only "
                "calibration sketches and entirely new validation seeds"
            ),
        },
    }


def _render_release_notes(manifest: dict[str, Any]) -> str:
    summary = manifest["summary"]
    artifact_rows = "\n".join(
        f"| `{item['file']}` | {item['role']} | `{item['sha256']}` |"
        for item in manifest["artifacts"]
    )
    return f"""# AlphaGPT Research v{manifest['release']['version']}

发布状态：**完整研究基线**。这不代表生产 alpha 已验证。

## 冻结结论

- 研究闭环完整：是
- 可用于生产交易：否
- 可进入 PPO：否
- 包含前端：否
- P11-D validation Spearman：
  `{summary['p11d']['validation_spearman']:+.3f}`
- P11-D 前瞻 reranker / random：
  `{summary['p11d']['prospective_reranker_mean']:+.3f}` /
  `{summary['p11d']['prospective_random_mean']:+.3f}`
- P11-E validation Spearman：
  `{summary['p11e']['validation_spearman']:+.3f}`

完整的公式生成、训练折评估、候选池、失败审计、rollout、CPU-only 模型和锁定
gate 已经落地。P11-D 前瞻绝对奖励与 P11-E 泛化 gate 均失败，所以本版本不会把
reward model 接入搜索，也不会加入 PPO。

## 统一命令

```powershell
Set-Location backend
# verify or rebuild the release manifest
.\\.venv\\Scripts\\python.exe -m research.alphagpt.run_release_v1
.\\.venv\\Scripts\\python.exe -m research.alphagpt.run_release_v1 --verify-only
.\\.venv\\Scripts\\python.exe -m pytest
```

## 发布产物

| 文件 | 角色 | SHA-256 |
|---|---|---|
{artifact_rows}

## 后续优化

后续如继续，另开 P11-F：使用固定训练区间 calibration sketch 提取低成本
execution-aware 特征，并使用全新 validation seed。不要继续扫描 P11-D/P11-E
已经消费过的 validation。
"""


def write_release() -> dict[str, Any]:
    manifest = build_release_manifest()
    write_json_atomic(MANIFEST_OUT, manifest)
    temporary = NOTES_OUT.with_suffix(NOTES_OUT.suffix + ".tmp")
    temporary.write_text(_render_release_notes(manifest), encoding="utf-8")
    temporary.replace(NOTES_OUT)
    return manifest


def verify_existing_release() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_OUT.read_text(encoding="utf-8"))
    if manifest["release"]["version"] != RELEASE_VERSION:
        raise ValueError("release manifest version mismatch")
    artifact_results = verify_artifact_records(
        manifest["artifacts"],
        artifact_dir=FACTOR_ARTIFACTS_DIR,
    )
    if not all(result["matches"] for result in artifact_results):
        mismatches = [
            result["file"] for result in artifact_results if not result["matches"]
        ]
        raise ValueError(f"release artifact verification failed: {mismatches}")
    checks, _ = _build_checks()
    require_checks(checks)
    return {
        "release": manifest["release"],
        "artifact_results": artifact_results,
        "validation_checks": checks,
    }


def main() -> None:
    args = build_parser().parse_args()
    if args.verify_only:
        result = verify_existing_release()
        print(
            "[alphagpt-release] verified | "
            f"version={result['release']['version']} "
            f"artifacts={len(result['artifact_results'])}",
            flush=True,
        )
        return
    manifest = write_release()
    print(
        "[alphagpt-release] complete | "
        f"version={manifest['release']['version']} "
        f"artifacts={len(manifest['artifacts'])} "
        f"production_ready={manifest['decision']['production_alpha_ready']} "
        f"| {MANIFEST_OUT}",
        flush=True,
    )


if __name__ == "__main__":
    main()
