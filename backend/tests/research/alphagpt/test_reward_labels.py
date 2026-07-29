from __future__ import annotations

import json

import pytest

from research.alphagpt.pool import formula_hash
from research.alphagpt.reward_labels import load_reward_labels


def _item(*, token: str, split: str, seed: int) -> dict:
    return {
        "formula_hash": formula_hash((token,)),
        "formula_tokens": [token],
        "intrinsic_reward": 1.0,
        "operational_reward": 0.9,
        "split": split,
        "data_seed": seed,
        "training_fold_metrics": [{"dataset_role": "train"}],
        "intrinsic_reward_breakdown": {"max_abs_correlation": 0.0},
    }


def test_reward_labels_require_seed_disjoint_train_validation(tmp_path) -> None:
    path = tmp_path / "labels.json"
    path.write_text(
        json.dumps(
            {
                "labels": [
                    _item(token="RET", split="train", seed=1),
                    _item(token="MOM5", split="validation", seed=1),
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="both train and validation"):
        load_reward_labels(path)


def test_reward_labels_load_valid_intrinsic_targets(tmp_path) -> None:
    path = tmp_path / "labels.json"
    path.write_text(
        json.dumps(
            {
                "labels": [
                    _item(token="RET", split="train", seed=1),
                    _item(token="MOM5", split="validation", seed=2),
                ]
            }
        ),
        encoding="utf-8",
    )
    labels = load_reward_labels(path)
    assert [label.intrinsic_reward for label in labels] == [1.0, 1.0]
    assert {label.data_seed for label in labels} == {1, 2}
