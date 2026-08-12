"""Run the P16 point-in-time universe correction against frozen P15 logic."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from research.paths import PROJECT_ROOT, SELECTION_ARTIFACTS_DIR, ensure_artifact_dirs
from research.selection.mvp_v2 import BASE_VARIANT, FACTOR_VARIANT
from research.selection.run_selection_mvp_v2 import run as run_selection_mvp_v2

REPORT = SELECTION_ARTIFACTS_DIR / "selection_pit_v1.json"
PIT_DETAIL = SELECTION_ARTIFACTS_DIR / "selection_pit_v1_detail.json"
PIT_LATEST_AUDIT = SELECTION_ARTIFACTS_DIR / "selection_pit_v1_latest_audit.csv"
PIT_DAILY_TOP = SELECTION_ARTIFACTS_DIR / "selection_pit_v1_daily_top20.csv"
FORWARD_PROTOCOL = SELECTION_ARTIFACTS_DIR / "selection_forward_watch_v1.json"
REGISTRATION_DATE = date(2026, 8, 12)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _primary(payload: Mapping[str, Any], variant: str) -> Mapping[str, Any]:
    return payload["walk_forward_test"]["candidate_primary_metrics"][variant]


def _delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, float | None]:
    keys = (
        "mean_net_return",
        "cohort_win_rate",
        "stock_win_rate",
        "mean_excess_return",
        "mean_top_bottom_spread",
        "mean_rank_ic",
    )
    output: dict[str, float | None] = {}
    for key in keys:
        left = before.get(key)
        right = after.get(key)
        output[key] = (
            round(float(right) - float(left), 8) if left is not None and right is not None else None
        )
    return output


def _forward_protocol(*, calibration_end: date, pit_report_hash: str) -> dict[str, Any]:
    protocol: dict[str, Any] = {
        "version": 1,
        "status": "PENDING_DATA",
        "registered_on": str(REGISTRATION_DATE),
        "calibration_end": str(calibration_end),
        "first_eligible_observation": f"first complete trading session after {calibration_end}",
        "minimum_sessions": 60,
        "target_sessions": 120,
        "observed_sessions": 0,
        "selector": BASE_VARIANT,
        "universe": "point_in_time_stock_st",
        "frozen_source_report_sha256": pit_report_hash,
        "auto_promote": False,
        "rules": [
            "do not change score thresholds, Top-K, holding horizon, costs or universe rules",
            "append only sessions strictly after calibration_end",
            "60 sessions unlock manual audit; they do not prove production readiness",
            "test results cannot reactivate the rejected factor overlay",
        ],
    }
    protocol["sha256"] = hashlib.sha256(
        json.dumps(protocol, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return protocol


def run(*, start: date = date(2024, 9, 24), end: date | None = None) -> dict[str, Any]:
    ensure_artifact_dirs()
    legacy = run_selection_mvp_v2(start=start, end=end, universe_mode="current_proxy")
    pit = run_selection_mvp_v2(
        start=start,
        end=end,
        universe_mode="point_in_time",
        report_path=PIT_DETAIL,
        latest_audit_path=PIT_LATEST_AUDIT,
        daily_top_path=PIT_DAILY_TOP,
    )
    legacy_base = _primary(legacy, BASE_VARIANT)
    pit_base = _primary(pit, BASE_VARIANT)
    legacy_factor = _primary(legacy, FACTOR_VARIANT)
    pit_factor = _primary(pit, FACTOR_VARIANT)
    legacy_latest = {row["symbol"] for row in legacy["latest_signal"]["top20"]}
    pit_latest = {row["symbol"] for row in pit["latest_signal"]["top20"]}

    base_excess = pit_base.get("mean_excess_return")
    selector_rejected = base_excess is None or float(base_excess) <= 0.0
    status = (
        "SELECTOR_REJECTED_HISTORICAL_EVIDENCE"
        if selector_rejected
        else "PIT_CORRECTION_COMPLETE_NO_PROMOTION"
    )
    detail_hash = _sha256(PIT_DETAIL)
    calibration_end = date.fromisoformat(str(pit["data"]["loaded_range"][1]))
    forward = _forward_protocol(
        calibration_end=calibration_end,
        pit_report_hash=detail_hash,
    )
    _atomic_json(FORWARD_PROTOCOL, forward)

    payload = {
        "status": status,
        "protocol": {
            "version": 1,
            "comparison": "same frozen P15 selector under current-name proxy vs daily stock_st",
            "parameters_changed": False,
            "test_metrics_used_for_tuning": False,
            "production_default_changed": False,
        },
        "data": {
            "range": pit["data"]["evaluation_range"],
            "stock_st": pit["data"]["stock_st"],
            "point_in_time_gap_closed_for_st": True,
            "remaining_gaps": [
                "stock_st identifies risk-warning membership but is not a full security-master history",
                "historical industry classification remains unavailable and excluded from scoring",
            ],
        },
        "universe_comparison": {
            "current_proxy": legacy["data"]["daily_dynamic_universe"],
            "point_in_time": pit["data"]["daily_dynamic_universe"],
            "latest_top20_overlap": len(legacy_latest & pit_latest),
            "latest_top20_added": sorted(pit_latest - legacy_latest),
            "latest_top20_removed": sorted(legacy_latest - pit_latest),
        },
        "primary_5d_top10": {
            BASE_VARIANT: {
                "current_proxy": legacy_base,
                "point_in_time": pit_base,
                "pit_minus_proxy": _delta(legacy_base, pit_base),
            },
            FACTOR_VARIANT: {
                "current_proxy": legacy_factor,
                "point_in_time": pit_factor,
                "pit_minus_proxy": _delta(legacy_factor, pit_factor),
            },
        },
        "factor_decision": {
            "point_in_time_train_votes": pit["next_observation_selector"]["train_fold_votes"],
            "selected_variant": pit["next_observation_selector"]["selected_variant"],
            "test_metrics_used": False,
        },
        "selector_lifecycle": {
            "quality_momentum_v1": (
                "rejected_historical_evidence" if selector_rejected else "experimental"
            ),
            "kept_executable_for_reproduction": True,
            "visible_by_default": False,
            "production_eligible": False,
        },
        "regime_diagnostic": pit["regime_diagnostic"],
        "latest_signal": pit["latest_signal"],
        "artifacts": {
            "pit_detail": _relative(PIT_DETAIL),
            "pit_detail_sha256": detail_hash,
            "pit_latest_audit": _relative(PIT_LATEST_AUDIT),
            "pit_latest_audit_sha256": _sha256(PIT_LATEST_AUDIT),
            "pit_daily_top20": _relative(PIT_DAILY_TOP),
            "pit_daily_top20_sha256": _sha256(PIT_DAILY_TOP),
            "forward_protocol": _relative(FORWARD_PROTOCOL),
            "forward_protocol_sha256": _sha256(FORWARD_PROTOCOL),
        },
        "next_step": {
            "action": "wait for frozen forward observations; do not optimize rejected selector",
            "earliest_calendar_date_hint": str(calibration_end + timedelta(days=1)),
            "fresh_oos_status": "NOT_STARTED",
        },
    }
    payload["protocol"]["sha256"] = hashlib.sha256(
        json.dumps(payload["protocol"], ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    _atomic_json(REPORT, payload)
    print(f"[selection-pit-v1] {status} -> {REPORT}", flush=True)
    return payload


if __name__ == "__main__":
    run()
