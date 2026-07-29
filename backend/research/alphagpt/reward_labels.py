"""P11-E 随机公式奖励标签的数据结构与审计读写。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research.alphagpt.pool import formula_hash


@dataclass(frozen=True)
class RewardLabel:
    formula_hash: str
    tokens: tuple[str, ...]
    intrinsic_reward: float
    operational_reward: float
    split: str
    data_seed: int


def load_reward_labels(path: Path) -> list[RewardLabel]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    labels: list[RewardLabel] = []
    seen: set[str] = set()
    for item in payload["labels"]:
        folds = item["training_fold_metrics"]
        if not folds or any(fold.get("dataset_role") != "train" for fold in folds):
            raise ValueError("reward label contains a non-training fold")
        split = str(item["split"])
        if split not in {"train", "validation"}:
            raise ValueError(f"unknown reward label split: {split}")
        tokens = tuple(item["formula_tokens"])
        digest = str(item["formula_hash"])
        if formula_hash(tokens) != digest:
            raise ValueError("reward label hash does not match formula tokens")
        if digest in seen:
            raise ValueError("duplicate formula in reward label dataset")
        seen.add(digest)
        intrinsic = float(item["intrinsic_reward"])
        operational = float(item["operational_reward"])
        if not math.isfinite(intrinsic) or not math.isfinite(operational):
            raise ValueError("reward labels must be finite")
        intrinsic_breakdown = item["intrinsic_reward_breakdown"]
        if float(intrinsic_breakdown["max_abs_correlation"]) != 0.0:
            raise ValueError("intrinsic reward must exclude pool correlation")
        labels.append(
            RewardLabel(
                formula_hash=digest,
                tokens=tokens,
                intrinsic_reward=intrinsic,
                operational_reward=operational,
                split=split,
                data_seed=int(item["data_seed"]),
            )
        )
    if not labels:
        raise ValueError("reward label dataset is empty")
    train_seeds = {label.data_seed for label in labels if label.split == "train"}
    validation_seeds = {
        label.data_seed for label in labels if label.split == "validation"
    }
    if not train_seeds or not validation_seeds:
        raise ValueError("reward label dataset requires train and validation seeds")
    if train_seeds & validation_seeds:
        raise ValueError("data seed appears in both train and validation")
    return labels


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)
