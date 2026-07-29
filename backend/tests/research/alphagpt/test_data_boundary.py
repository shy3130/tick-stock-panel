from __future__ import annotations

from datetime import date

import pytest

from research.alphagpt.run_alphagpt_v1 import (
    FoldDataset,
    FoldSpec,
    TrainingEvaluator,
    evaluate_holdout,
)


def test_training_evaluator_rejects_holdout_dataset() -> None:
    holdout = FoldDataset(
        spec=FoldSpec("HOLDOUT", date(2026, 2, 1), date(2026, 6, 30), "holdout"),
        features={},
    )
    with pytest.raises(ValueError, match="cannot receive holdout"):
        TrainingEvaluator([holdout])


def test_holdout_evaluator_rejects_training_dataset() -> None:
    training = FoldDataset(
        spec=FoldSpec("T1", date(2025, 1, 1), date(2025, 3, 31), "train"),
        features={},
    )
    with pytest.raises(ValueError, match="sealed holdout"):
        evaluate_holdout(["MOM20"], training)
