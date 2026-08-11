"""Observe the frozen P16 selector on sessions after its calibration cutoff."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from research.paths import DATA_DIR, SELECTION_ARTIFACTS_DIR, ensure_artifact_dirs
from research.selection.run_selection_mvp_v2 import run as run_selection_mvp_v2
from research.selection.run_selection_pit_v1 import FORWARD_PROTOCOL

OBSERVATIONS = SELECTION_ARTIFACTS_DIR / "selection_forward_watch_v1_observations.json"
DETAIL = SELECTION_ARTIFACTS_DIR / "selection_forward_watch_v1_detail.json"
LATEST_AUDIT = SELECTION_ARTIFACTS_DIR / "selection_forward_watch_v1_latest_audit.csv"
DAILY_TOP = SELECTION_ARTIFACTS_DIR / "selection_forward_watch_v1_daily_top20.csv"


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _protocol_hash(payload: Mapping[str, Any]) -> str:
    content = {key: value for key, value in payload.items() if key != "sha256"}
    return hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _latest_local_date() -> date:
    labels: list[date] = []
    for path in (DATA_DIR / "kline_daily_enriched").glob("date=*"):
        try:
            labels.append(date.fromisoformat(path.name.removeprefix("date=")))
        except ValueError:
            continue
    if not labels:
        raise ValueError("no enriched daily partitions are available")
    return max(labels)


def _summarize_rows(
    rows: Sequence[Mapping[str, Any]], *, cutoff: date, top_k: int = 10
) -> dict[str, Any]:
    grouped: dict[str, list[float | None]] = defaultdict(list)
    for row in rows:
        signal_date = str(row.get("signal_date") or "")
        if not signal_date or date.fromisoformat(signal_date) <= cutoff:
            continue
        if int(row.get("rank") or 0) > top_k:
            continue
        raw_return = row.get("return_5d_net")
        try:
            parsed = float(raw_return) if raw_return not in (None, "") else None
        except (TypeError, ValueError):
            parsed = None
        grouped[signal_date].append(parsed)

    cohorts: list[dict[str, Any]] = []
    stock_returns: list[float] = []
    for signal_date, values in sorted(grouped.items()):
        valid = [float(value) for value in values if value is not None and np.isfinite(value)]
        stock_returns.extend(valid)
        cohorts.append(
            {
                "signal_date": signal_date,
                "selected": len(values),
                "valid": len(valid),
                "coverage": round(len(valid) / len(values), 6) if values else 0.0,
                "mean_net_return": round(float(np.mean(valid)), 8) if valid else None,
            }
        )
    usable = [
        float(row["mean_net_return"]) for row in cohorts if row["mean_net_return"] is not None
    ]
    return {
        "observed_sessions": len(cohorts),
        "usable_cohorts": len(usable),
        "mean_5d_net_return": round(float(np.mean(usable)), 8) if usable else None,
        "cohort_win_rate": (
            round(sum(value > 0 for value in usable) / len(usable), 6) if usable else None
        ),
        "stock_win_rate": (
            round(sum(value > 0 for value in stock_returns) / len(stock_returns), 6)
            if stock_returns
            else None
        ),
        "cohorts": cohorts,
    }


def run() -> dict[str, Any]:
    ensure_artifact_dirs()
    if not FORWARD_PROTOCOL.exists():
        raise ValueError("frozen selection forward protocol is missing; run P16 first")
    protocol = json.loads(FORWARD_PROTOCOL.read_text(encoding="utf-8"))
    if protocol.get("sha256") != _protocol_hash(protocol):
        raise ValueError("selection forward protocol hash mismatch")
    cutoff = date.fromisoformat(str(protocol["calibration_end"]))
    latest = _latest_local_date()
    summary: dict[str, Any]
    if latest <= cutoff:
        summary = {
            "observed_sessions": 0,
            "usable_cohorts": 0,
            "mean_5d_net_return": None,
            "cohort_win_rate": None,
            "stock_win_rate": None,
            "cohorts": [],
        }
    else:
        run_selection_mvp_v2(
            universe_mode="point_in_time",
            report_path=DETAIL,
            latest_audit_path=LATEST_AUDIT,
            daily_top_path=DAILY_TOP,
        )
        with DAILY_TOP.open("r", encoding="utf-8-sig", newline="") as handle:
            summary = _summarize_rows(list(csv.DictReader(handle)), cutoff=cutoff)

    observed = int(summary["observed_sessions"])
    status = (
        "READY_FOR_MANUAL_AUDIT"
        if observed >= int(protocol["minimum_sessions"])
        else "PENDING_DATA"
    )
    payload = {
        "status": status,
        "protocol_sha256": protocol["sha256"],
        "calibration_end": str(cutoff),
        "latest_local_session": str(latest),
        "minimum_sessions": int(protocol["minimum_sessions"]),
        "target_sessions": int(protocol["target_sessions"]),
        "remaining_to_minimum": max(int(protocol["minimum_sessions"]) - observed, 0),
        "remaining_to_target": max(int(protocol["target_sessions"]) - observed, 0),
        "metrics": summary,
        "auto_promote": False,
        "production_default_changed": False,
        "note": (
            "Observation starts strictly after calibration_end. Reaching the minimum only "
            "unlocks manual audit and cannot reactivate the rejected factor overlay."
        ),
    }
    _atomic_json(OBSERVATIONS, payload)
    print(f"[selection-forward-watch-v1] {status} -> {OBSERVATIONS}", flush=True)
    return payload


if __name__ == "__main__":
    run()
