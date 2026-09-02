from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from app.services.position_analysis_learning import (
    PositionLearningFeedback,
    active_scenario_priors,
    apply_candidate,
    list_candidates,
    record_feedback,
    rollback_candidate,
)


def feedback(index: int, *, outcome: str = "weak") -> PositionLearningFeedback:
    return PositionLearningFeedback(
        observation_id=f"pa-{index:016x}",
        trade_date=date(2026, 8, 1) + timedelta(days=index),
        symbol="600519.SH",
        outcome=outcome,
        evidence_grade="B",
        note="收盘后按日 K 与正式 EOD 资金流复核",
    )


def test_feedback_is_idempotent_and_does_not_auto_apply(tmp_path):
    first = record_feedback(tmp_path, feedback(1))
    duplicate = record_feedback(tmp_path, feedback(1))
    assert first == {"recorded": True, "candidate": None}
    assert duplicate["recorded"] is False
    assert duplicate["reason"] == "duplicate_observation"
    assert active_scenario_priors(tmp_path) is None
    audit_text = (
        tmp_path
        / "user_data"
        / "position_analysis_agent"
        / "learning"
        / "feedback.jsonl"
    ).read_text()
    assert "收盘后按日 K" not in audit_text
    assert "note_digest" in audit_text


def test_candidate_requires_five_distinct_trade_days(tmp_path):
    latest = None
    for index in range(10):
        latest = record_feedback(
            tmp_path,
            feedback(index).model_copy(update={"trade_date": date(2026, 8, 1)}),
        )
    assert latest["candidate"] is None


def test_ten_distinct_outcomes_validate_but_require_explicit_apply(tmp_path):
    latest = None
    for index in range(10):
        latest = record_feedback(tmp_path, feedback(index))
    candidate = latest["candidate"]
    assert candidate["status"] == "validated"
    assert candidate["sample_size"] == 10
    assert candidate["validation_size"] == 2
    assert candidate["candidate_brier"] < candidate["baseline_brier"]
    assert active_scenario_priors(tmp_path) is None

    applied = apply_candidate(tmp_path, candidate["id"])
    priors = active_scenario_priors(tmp_path)
    assert applied["status"] == "applied"
    assert priors == candidate["proposed_priors"]
    assert apply_candidate(tmp_path, candidate["id"])["status"] == "applied"
    assert priors["weak"] > priors["repair"]
    assert list_candidates(tmp_path)[0]["status"] == "applied"

    rolled_back = rollback_candidate(tmp_path, candidate["id"])
    assert rolled_back["status"] == "rolled_back"
    assert rollback_candidate(tmp_path, candidate["id"])["status"] == "rolled_back"
    assert active_scenario_priors(tmp_path) is None


def test_feedback_schema_cannot_evolve_hard_risk_thresholds():
    with pytest.raises(ValidationError):
        PositionLearningFeedback.model_validate(
            {
                **feedback(1).model_dump(mode="json"),
                "price_anomaly_ratio": 0.50,
            }
        )
