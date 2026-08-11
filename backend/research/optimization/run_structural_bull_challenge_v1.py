"""Audit a high-win/high-return structural-bull calibration challenge.

This experiment deliberately separates two claims:

* whether a small, pre-declared grid can hit the user's requested 60% win rate
  and 80% return inside the already-observed 2026 structural-bull window; and
* whether the frozen winner generalizes to two previously documented strong-bull
  windows and the later observation window.

The calibration window has already been inspected repeatedly.  Its winner is
therefore in-sample evidence only and can never be promoted automatically.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import date
from typing import Any

from app.backtest.strategy import StrategyBacktestConfig
from research.common.universe import symbols_sha256
from research.optimization.run_core_portfolio_walkforward_v1 import (
    TRIAL_POLICY,
    _make_service,
    _result_stats,
)
from research.optimization.run_core_strategy_walkforward_v1 import BACKTEST_KWARGS
from research.paths import DATA_DIR, OPTIMIZATION_ARTIFACTS_DIR, ensure_artifact_dirs
from scripts.run_mvp import select_universe


OUT = OPTIMIZATION_ARTIFACTS_DIR / "structural_bull_challenge_v1.json"
STRATEGY_ID = "bullish_alignment"
WIN_RATE_TARGET = 0.60
RETURN_TARGET = 0.80
MIN_TRADES = 30

CALIBRATION = ("target_2026", date(2026, 3, 24), date(2026, 6, 24))
VALIDATION_WINDOWS = (
    ("leader_bull_2025_a", date(2025, 5, 24), date(2025, 8, 24), True),
    ("leader_bull_2025_b", date(2025, 7, 24), date(2025, 10, 24), True),
    ("post_target_audit", date(2026, 6, 25), date(2026, 8, 7), False),
)

TAKE_PROFITS = (0.055, 0.060, 0.065, 0.070, 0.075, 0.080)
MAX_POSITIONS = (4, 5, 6, 7, 8)
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 20


def candidates() -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": f"tp{take_profit:.3f}_equal_score_{max_positions}",
            "take_profit": take_profit,
            "stop_loss": STOP_LOSS,
            "max_hold_days": MAX_HOLD_DAYS,
            "max_positions": max_positions,
            "position_sizing": "score_weight",
        }
        for take_profit in TAKE_PROFITS
        for max_positions in MAX_POSITIONS
    )


def target_met(stats: dict[str, Any] | None) -> bool:
    if not stats or int(stats.get("n_trades", 0)) < MIN_TRADES:
        return False
    return (
        float(stats.get("win_rate", 0.0)) >= WIN_RATE_TARGET
        and float(stats.get("total_return", 0.0)) >= RETURN_TARGET
    )


def joint_score(stats: dict[str, Any] | None) -> float:
    if not stats or int(stats.get("n_trades", 0)) < MIN_TRADES:
        return float("-inf")
    win_rate = float(stats.get("win_rate", 0.0))
    total_return = float(stats.get("total_return", 0.0))
    max_drawdown = float(stats.get("max_drawdown", 0.0))
    if not all(math.isfinite(value) for value in (win_rate, total_return, max_drawdown)):
        return float("-inf")
    # The weaker normalized target controls the score.  Drawdown is only a
    # deterministic tie-break, not a hidden third optimization objective.
    return min(win_rate / WIN_RATE_TARGET, total_return / RETURN_TARGET)


def select_calibration_winner(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in rows if math.isfinite(joint_score(row.get("stats")))]
    if not eligible:
        raise ValueError("calibration produced no eligible candidate")
    return sorted(
        eligible,
        key=lambda row: (
            not target_met(row.get("stats")),
            -joint_score(row.get("stats")),
            abs(float(row["stats"]["max_drawdown"])),
            str(row["candidate"]["id"]),
        ),
    )[0]


def strict_bull_generalization_pass(rows: list[dict[str, Any]]) -> bool:
    bull_rows = [row for row in rows if row.get("is_predeclared_bull_window")]
    return bool(bull_rows) and all(target_met(row.get("stats")) for row in bull_rows)


def _config(
    symbols: list[str],
    start: date,
    end: date,
    candidate: dict[str, Any] | None,
) -> StrategyBacktestConfig:
    kwargs = dict(BACKTEST_KWARGS)
    if candidate is None:
        kwargs["max_positions"] = 10
        kwargs["position_sizing"] = "equal"
        overrides = None
    else:
        kwargs["max_positions"] = int(candidate["max_positions"])
        kwargs["position_sizing"] = str(candidate["position_sizing"])
        overrides = {
            "take_profit": float(candidate["take_profit"]),
            "stop_loss": float(candidate["stop_loss"]),
            "max_hold_days": int(candidate["max_hold_days"]),
        }
    return StrategyBacktestConfig(
        strategy_id=STRATEGY_ID,
        symbols=symbols,
        start=start,
        end=end,
        params=None,
        overrides=overrides,
        **kwargs,
    )


def _run_one(service, config: StrategyBacktestConfig, market_data=None) -> tuple[dict[str, Any], Any]:
    prepared = service.prepare_matrix_optimization(
        [config],
        market_data_override=market_data,
    )
    stats, error, status = _result_stats(
        service.run(config, prepared=prepared, result_policy=TRIAL_POLICY)
    )
    row: dict[str, Any] = {"status": status, "stats": stats}
    if error is not None:
        row["error"] = error
    return row, prepared.market_data


def _window_manifest(symbols: list[str], start: date, end: date) -> dict[str, Any]:
    return {
        "start": str(start),
        "end": str(end),
        "selection": "all end-date listed non-ST stocks; no sampling",
        "size": len(symbols),
        "sha256": symbols_sha256(symbols),
    }


def _protocol() -> dict[str, Any]:
    protocol = {
        "version": 1,
        "strategy_id": STRATEGY_ID,
        "calibration": [str(CALIBRATION[1]), str(CALIBRATION[2])],
        "calibration_evidence": "in_sample_already_observed",
        "validation_windows": [
            {
                "id": window_id,
                "range": [str(start), str(end)],
                "is_predeclared_bull_window": is_bull,
            }
            for window_id, start, end, is_bull in VALIDATION_WINDOWS
        ],
        "targets": {
            "win_rate_min": WIN_RATE_TARGET,
            "total_return_min": RETURN_TARGET,
            "minimum_trades": MIN_TRADES,
        },
        "candidate_budget": len(candidates()),
        "candidate_grid": {
            "take_profit": TAKE_PROFITS,
            "stop_loss": (STOP_LOSS,),
            "max_hold_days": (MAX_HOLD_DAYS,),
            "max_positions": MAX_POSITIONS,
            "position_sizing": ("score_weight",),
        },
        "selection": (
            "target-met candidates first; then maximize the weaker normalized target; "
            "then lower absolute drawdown and lexical candidate id"
        ),
        "leakage_warning": (
            "The 2026 target window selects the candidate and is not OOS. Validation "
            "windows never change the selected candidate. No result can auto-promote."
        ),
        "backtest_kwargs": BACKTEST_KWARGS,
    }
    protocol["sha256"] = hashlib.sha256(
        json.dumps(protocol, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return protocol


def _atomic_write(payload: dict[str, Any]) -> None:
    ensure_artifact_dirs()
    temporary = OUT.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(OUT)


def main() -> dict[str, Any]:
    started = time.time()
    protocol = _protocol()
    calibration_id, calibration_start, calibration_end = CALIBRATION
    calibration_symbols = select_universe(
        DATA_DIR,
        start=calibration_start,
        end=calibration_end,
        size=None,
        seed=None,
    )
    failures: list[dict[str, Any]] = []
    store, service = _make_service()
    try:
        calibration_rows: list[dict[str, Any]] = []
        market_data = None
        for candidate in candidates():
            result, market_data = _run_one(
                service,
                _config(
                    calibration_symbols,
                    calibration_start,
                    calibration_end,
                    candidate,
                ),
                market_data,
            )
            row = {"candidate": candidate, **result}
            row["target_met"] = target_met(row.get("stats"))
            row["joint_score"] = (
                round(joint_score(row.get("stats")), 6)
                if math.isfinite(joint_score(row.get("stats")))
                else None
            )
            calibration_rows.append(row)
            if result["status"] == "error":
                failures.append({
                    "phase": "calibration",
                    "candidate_id": candidate["id"],
                    "error": result.get("error"),
                })

        winner = select_calibration_winner(calibration_rows)
        selected = dict(winner["candidate"])
        validation_rows: list[dict[str, Any]] = []
        for window_id, start, end, is_bull in VALIDATION_WINDOWS:
            symbols = select_universe(
                DATA_DIR,
                start=start,
                end=end,
                size=None,
                seed=None,
            )
            baseline, validation_market = _run_one(
                service,
                _config(symbols, start, end, None),
            )
            challenge, _ = _run_one(
                service,
                _config(symbols, start, end, selected),
                validation_market,
            )
            row = {
                "id": window_id,
                "is_predeclared_bull_window": is_bull,
                "universe": _window_manifest(symbols, start, end),
                "baseline": baseline,
                "challenge": challenge,
                "stats": challenge.get("stats"),
                "target_met": target_met(challenge.get("stats")),
            }
            validation_rows.append(row)
            for role, result in (("baseline", baseline), ("challenge", challenge)):
                if result["status"] == "error":
                    failures.append({
                        "phase": "validation",
                        "window_id": window_id,
                        "role": role,
                        "error": result.get("error"),
                    })
    finally:
        store.db.close()

    generalizes = strict_bull_generalization_pass(validation_rows)
    payload = {
        "status": "REJECTED_OVERFIT" if not generalizes else "HISTORICAL_REPLAY_ONLY",
        "protocol": protocol,
        "calibration": {
            "id": calibration_id,
            "universe": _window_manifest(
                calibration_symbols,
                calibration_start,
                calibration_end,
            ),
            "trials": calibration_rows,
            "selected_candidate": selected,
            "selected_stats": winner.get("stats"),
            "target_met_in_sample": target_met(winner.get("stats")),
        },
        "validation": validation_rows,
        "strict_bull_generalization_pass": generalizes,
        "production_changed": False,
        "failures": failures,
        "elapsed_seconds": round(time.time() - started, 1),
        "conclusion": (
            "The requested thresholds were found only in the inspected calibration window; "
            "the frozen candidate failed both predeclared 2025 bull windows."
            if not generalizes
            else "Historical bull windows met the requested thresholds; fresh OOS is still required."
        ),
    }
    _atomic_write(payload)
    print(
        f"[structural-bull-challenge] {payload['status']} "
        f"selected={selected['id']} -> {OUT}",
        flush=True,
    )
    return payload


if __name__ == "__main__":
    main()
