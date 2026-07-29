"""Train-only exit/risk-control search for the five core strategies.

The seven candidates are pre-registered and receive identical budgets.  All
historical test folds are evaluation-only and already reused evidence, so even
a passing result can only seed an observation beginning after 2026-07-21.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from collections import Counter
from datetime import date
from typing import Any

from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.walkforward import generate_folds
from research.common.universe import universe_manifest
from research.optimization.run_core_portfolio_walkforward_v1 import (
    TRIAL_POLICY,
    _make_service,
    _result_stats,
    training_score,
)
from research.optimization.run_core_strategy_walkforward_v1 import (
    BACKTEST_KWARGS,
    END,
    N_SYMBOLS,
    SEED,
    START,
    STEP_DAYS,
    TEST_DAYS,
    TRAIN_DAYS,
    compare_to_default,
    select_universe,
)
from research.paths import OPTIMIZATION_ARTIFACTS_DIR, ensure_artifact_dirs


OUT = OPTIMIZATION_ARTIFACTS_DIR / "core_exit_walkforward_v1.json"
DEFAULT_CANDIDATE_ID = "default"
FUTURE_OBSERVATION_START = date(2026, 7, 22)
V1_STRATEGY_IDS = frozenset({
    "bullish_alignment",
    "trend_breakout",
    "pullback_to_support",
    "oversold_reversal",
    "limit_up_momentum",
})

EXIT_CANDIDATES: tuple[dict[str, Any], ...] = (
    {"id": "default", "overrides": None},
    {"id": "hold_30", "overrides": {"max_hold_days": 30}},
    {"id": "hold_8", "overrides": {"max_hold_days": 8}},
    {"id": "stop_10", "overrides": {"stop_loss": -0.10}},
    {"id": "stop_3", "overrides": {"stop_loss": -0.03}},
    {"id": "stop_4_hold_12", "overrides": {"stop_loss": -0.04, "max_hold_days": 12}},
    {
        "id": "trailing_12_6",
        "overrides": {
            "trailing_take_profit_activate": 0.12,
            "trailing_take_profit_drawdown": 0.06,
        },
    },
)


def validate_candidates() -> None:
    ids = [candidate["id"] for candidate in EXIT_CANDIDATES]
    if len(ids) != len(set(ids)):
        raise ValueError("exit candidate IDs must be unique")
    if DEFAULT_CANDIDATE_ID not in ids:
        raise ValueError("explicit default candidate is required")
    allowed = {
        "stop_loss", "max_hold_days", "trailing_take_profit_activate",
        "trailing_take_profit_drawdown",
    }
    for candidate in EXIT_CANDIDATES:
        unknown = set(candidate.get("overrides") or {}) - allowed
        if unknown:
            raise ValueError(f"unsupported exit overrides: {sorted(unknown)}")


def select_training_candidate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [
        row
        for row in rows
        if isinstance(row.get("training_score"), (int, float))
        and math.isfinite(float(row["training_score"]))
    ]
    if not eligible:
        default = next(
            (row for row in rows if row.get("candidate", {}).get("id") == DEFAULT_CANDIDATE_ID),
            None,
        )
        if default is None:
            raise ValueError("no eligible exit candidate and no explicit default")
        return {**default, "selection_fallback": "default_no_eligible_candidate"}
    return sorted(
        eligible,
        key=lambda row: (
            -float(row["training_score"]),
            -float(row["stats"]["total_return"]),
            -float(row["stats"]["sharpe"]),
            str(row["candidate"]["id"]),
        ),
    )[0]


def next_training_mode_candidate(folds: list[dict[str, Any]]) -> dict[str, Any] | None:
    ids = [
        fold["selected_candidate"]["id"]
        for fold in folds
        if fold.get("selected_candidate") is not None
    ]
    if not ids:
        return None
    counts = Counter(ids)
    selected_id, frequency = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    candidate = next(item for item in EXIT_CANDIDATES if item["id"] == selected_id)
    return {
        "candidate": dict(candidate),
        "selected_train_folds": frequency,
        "selection_share": round(frequency / len(ids), 4),
        "selection_method": "mode of per-fold train-only winners; lexical tie-break",
    }


def _protocol(symbols: list[str]) -> dict[str, Any]:
    folds = generate_folds(START, END, TRAIN_DAYS, TEST_DAYS, STEP_DAYS)
    return {
        "version": 1,
        "evidence_status": "canonical_historical_replay_not_fresh_oos",
        "scope": "exit/risk controls only; entry signals and strategy params unchanged",
        "seed": SEED,
        "universe_manifest": universe_manifest(
            symbols,
            seed=SEED,
            requested_size=N_SYMBOLS,
            start=START,
            end=END,
        ),
        "fold_config": {
            "train_calendar_days": TRAIN_DAYS,
            "test_calendar_days": TEST_DAYS,
            "step_calendar_days": STEP_DAYS,
            "folds": [
                {
                    "index": fold.index,
                    "train": [str(fold.train_start), str(fold.train_end)],
                    "test": [str(fold.test_start), str(fold.test_end)],
                }
                for fold in folds
            ],
        },
        "objective": {
            "formula": "training total_return - 0.5 * abs(training max_drawdown)",
            "minimum_training_trades": 30,
            "tie_break": ["total_return", "sharpe", "candidate_id lexical"],
        },
        "candidates": EXIT_CANDIDATES,
        "candidate_count_per_strategy_per_fold": len(EXIT_CANDIDATES),
        "backtest_kwargs": BACKTEST_KWARGS,
        "no_signal_policy": "explicit cash: zero return, zero drawdown, zero trades",
        "promotion_rule": {
            "positive_optimized_fold_ratio_min": 0.6,
            "beat_default_fold_ratio_min": 0.6,
            "mean_return_delta_min": 0.0,
            "compounded_return_delta_min": 0.0,
            "mean_drawdown_tolerance": 0.03,
            "minimum_total_oos_trades": 50,
            "auto_apply": False,
        },
        "fresh_observation_boundary": {
            "earliest_start": str(FUTURE_OBSERVATION_START),
            "reason": "2026-07-01 through 2026-07-21 has already been observed",
        },
    }


def _protocol_hash(protocol: dict[str, Any]) -> str:
    encoded = json.dumps(protocol, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _atomic_write(payload: dict[str, Any]) -> None:
    ensure_artifact_dirs()
    temporary = OUT.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(OUT)


def _config(
    strategy_id: str,
    symbols: list[str],
    start: date,
    end: date,
    candidate: dict[str, Any],
) -> StrategyBacktestConfig:
    return StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=symbols,
        start=start,
        end=end,
        params=None,
        overrides=candidate.get("overrides"),
        **BACKTEST_KWARGS,
    )


def _run_one(service, config: StrategyBacktestConfig, market_data) -> dict[str, Any]:
    prepared = service.prepare_matrix_optimization(
        [config],
        market_data_override=market_data,
    )
    stats, error, status = _result_stats(
        service.run(config, prepared=prepared, result_policy=TRIAL_POLICY)
    )
    row: dict[str, Any] = {"status": status}
    if error is not None:
        row["error"] = error
    else:
        row["stats"] = stats
    return row


def _run_strategy(service, strategy_id: str, symbols: list[str]) -> dict[str, Any]:
    # Load the widest required historical matrix once. Fold/candidate preparations
    # become read-only time views and still resolve their own override semantics.
    anchor = _config(
        strategy_id,
        symbols,
        START,
        END,
        {"id": "anchor", "overrides": {"max_hold_days": 30}},
    )
    base = service.prepare_matrix_optimization([anchor])
    folds = generate_folds(START, END, TRAIN_DAYS, TEST_DAYS, STEP_DAYS)
    fold_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    default_candidate = next(
        candidate for candidate in EXIT_CANDIDATES if candidate["id"] == DEFAULT_CANDIDATE_ID
    )

    for fold in folds:
        training_rows: list[dict[str, Any]] = []
        for candidate in EXIT_CANDIDATES:
            result = _run_one(
                service,
                _config(strategy_id, symbols, fold.train_start, fold.train_end, candidate),
                base.market_data,
            )
            row = {"candidate": dict(candidate), "status": result["status"]}
            if "error" in result:
                row["error"] = result["error"]
                failures.append({
                    "strategy_id": strategy_id,
                    "fold": fold.index,
                    "phase": "train",
                    "candidate_id": candidate["id"],
                    "error": result["error"],
                })
            else:
                stats = result["stats"]
                score = training_score(stats)
                row["stats"] = stats
                row["training_score"] = score if math.isfinite(score) else None
                if not math.isfinite(score):
                    row["ineligible_reason"] = (
                        "training trades below 30 or non-finite metrics"
                    )
            training_rows.append(row)

        selected = select_training_candidate(training_rows)
        selected_candidate = selected["candidate"]
        selected_test = _run_one(
            service,
            _config(strategy_id, symbols, fold.test_start, fold.test_end, selected_candidate),
            base.market_data,
        )
        default_test = _run_one(
            service,
            _config(strategy_id, symbols, fold.test_start, fold.test_end, default_candidate),
            base.market_data,
        )
        for role, candidate, result in (
            ("selected", selected_candidate, selected_test),
            ("default", default_candidate, default_test),
        ):
            if "error" in result:
                failures.append({
                    "strategy_id": strategy_id,
                    "fold": fold.index,
                    "phase": "test",
                    "role": role,
                    "candidate_id": candidate["id"],
                    "error": result["error"],
                })

        fold_row: dict[str, Any] = {
            "index": fold.index,
            "train": [str(fold.train_start), str(fold.train_end)],
            "test": [str(fold.test_start), str(fold.test_end)],
            "training_trials": training_rows,
            "selected_candidate": dict(selected_candidate),
            "selected_training": {
                key: selected[key]
                for key in ("status", "stats", "training_score", "selection_fallback")
                if key in selected
            },
            "test_selected": selected_test,
            "test_default": default_test,
        }
        if "stats" in selected_test:
            fold_row["best_params"] = dict(selected_candidate)
            fold_row["oos_stats"] = selected_test["stats"]
        fold_rows.append(fold_row)

    default_folds = [
        {"index": fold["index"], "stats": fold["test_default"]["stats"]}
        if "stats" in fold["test_default"]
        else {"index": fold["index"], "error": fold["test_default"].get("error")}
        for fold in fold_rows
    ]
    comparison = compare_to_default(fold_rows, default_folds, len(folds))
    mode_candidate = next_training_mode_candidate(fold_rows)
    return {
        "candidate_count": len(EXIT_CANDIDATES),
        "training_evaluation_budget": len(EXIT_CANDIDATES) * len(folds),
        "folds": fold_rows,
        "comparison": comparison,
        "next_training_mode_candidate": mode_candidate,
        "future_watch_candidate": (
            {
                **(mode_candidate or {}),
                "observation_start": str(FUTURE_OBSERVATION_START),
                "status": "WATCH_ONLY_NOT_PRODUCTION",
            }
            if comparison["status"] == "CANDIDATE_FOR_FUTURE_FROZEN_OOS"
            else None
        ),
        "failures": failures,
    }


def main(*, resume: bool = False) -> dict[str, Any]:
    started = time.time()
    validate_candidates()
    symbols = select_universe()
    protocol = _protocol(symbols)
    digest = _protocol_hash(protocol)
    strategies: dict[str, Any] = {}
    previous_elapsed_seconds: float | None = None
    if resume and OUT.exists():
        existing = json.loads(OUT.read_text(encoding="utf-8"))
        if existing.get("protocol_hash") != digest:
            raise ValueError("existing checkpoint protocol does not match current config")
        strategies = dict(existing.get("strategies") or {})
        if existing.get("status") == "complete":
            previous_elapsed_seconds = existing.get("elapsed_seconds")

    evaluated_count = 0
    store, service = _make_service()
    try:
        print(
            f"[core-exit] universe={len(symbols)} folds=7 strategies={len(V1_STRATEGY_IDS)} "
            f"candidates={len(EXIT_CANDIDATES)} resume={resume}",
            flush=True,
        )
        for strategy_id in sorted(V1_STRATEGY_IDS):
            if strategy_id in strategies:
                print(f"[core-exit] skip {strategy_id}", flush=True)
                continue
            evaluated_count += 1
            try:
                result = _run_strategy(service, strategy_id, symbols)
                strategies[strategy_id] = result
                comparison = result["comparison"]
                print(
                    f"[core-exit] {strategy_id} {comparison['status']} "
                    f"selected={comparison['optimized']['compounded_return']:+.2%} "
                    f"default={comparison['default']['compounded_return']:+.2%}",
                    flush=True,
                )
            except Exception as exc:
                strategies[strategy_id] = {
                    "status": "FAILED_EXPLICITLY",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                print(f"[core-exit] FAIL {strategy_id}: {exc}", flush=True)
            _atomic_write({
                "protocol_hash": digest,
                "protocol": protocol,
                "strategies": strategies,
                "status": "running",
            })
    finally:
        store.db.close()

    elapsed_seconds = round(time.time() - started, 1)
    if evaluated_count == 0 and previous_elapsed_seconds is not None:
        elapsed_seconds = previous_elapsed_seconds
    payload = {
        "protocol_hash": digest,
        "protocol": protocol,
        "strategies": strategies,
        "status": "complete",
        "elapsed_seconds": elapsed_seconds,
        "promotion_summary": {
            strategy_id: row.get("comparison", {}).get("status", row.get("status"))
            for strategy_id, row in strategies.items()
        },
        "note": "Historical replay only. No production strategy or default was changed.",
    }
    _atomic_write(payload)
    print(f"[core-exit] complete -> {OUT}", flush=True)
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    main(resume=args.resume)
