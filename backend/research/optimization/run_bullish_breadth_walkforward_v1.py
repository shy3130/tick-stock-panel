"""Fixed three-candidate market-breadth study for bullish_alignment.

This is a historical replay over previously seen folds.  Breadth uses the
configured universe, prior-day close versus MA20/MA60, and hysteresis.  A pass
can only create a watch candidate beginning after 2026-07-21.
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

OUT = OPTIMIZATION_ARTIFACTS_DIR / "bullish_breadth_walkforward_v1.json"
STRATEGY_ID = "bullish_alignment"
DEFAULT_CANDIDATE_ID = "default"
FUTURE_OBSERVATION_START = date(2026, 7, 22)

BREADTH_CANDIDATES: tuple[dict[str, Any], ...] = (
    {"id": "default", "regime_filter": None},
    {
        "id": "breadth_balanced_soft_30",
        "regime_filter": {
            "type": "market_breadth",
            "mode": "soft",
            "enter_ma20": 0.55,
            "enter_ma60": 0.50,
            "exit_ma20": 0.45,
            "exit_ma60": 0.40,
            "min_valid_assets": 20,
            "bear_weight": 0.30,
            "scale_existing": True,
        },
    },
    {
        "id": "breadth_conservative_cash",
        "regime_filter": {
            "type": "market_breadth",
            "mode": "soft",
            "enter_ma20": 0.60,
            "enter_ma60": 0.55,
            "exit_ma20": 0.50,
            "exit_ma60": 0.45,
            "min_valid_assets": 20,
            "bear_weight": 0.0,
            "scale_existing": True,
        },
    },
)


def validate_candidates() -> None:
    ids = [candidate["id"] for candidate in BREADTH_CANDIDATES]
    if len(ids) != 3 or len(set(ids)) != 3:
        raise ValueError("breadth v1 must contain exactly three unique candidates")
    if ids.count(DEFAULT_CANDIDATE_ID) != 1:
        raise ValueError("breadth v1 requires one explicit default")
    for candidate in BREADTH_CANDIDATES:
        regime_filter = candidate.get("regime_filter")
        if regime_filter is not None and regime_filter.get("type") != "market_breadth":
            raise ValueError("breadth candidates may only use market_breadth filters")


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
            raise ValueError("no eligible breadth candidate and no explicit default")
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
    candidate = next(item for item in BREADTH_CANDIDATES if item["id"] == selected_id)
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
        "scope": "bullish_alignment market breadth exposure only",
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
        },
        "lookahead_defense": (
            "day t exposure state uses breadth observed at t-1; warmup history seeds hysteresis"
        ),
        "candidates": BREADTH_CANDIDATES,
        "candidate_count_per_fold": len(BREADTH_CANDIDATES),
        "backtest_kwargs": BACKTEST_KWARGS,
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
            "reason": "all data through 2026-07-21 has already been observed",
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
    symbols: list[str],
    start: date,
    end: date,
    candidate: dict[str, Any],
) -> StrategyBacktestConfig:
    return StrategyBacktestConfig(
        strategy_id=STRATEGY_ID,
        symbols=symbols,
        start=start,
        end=end,
        params=None,
        overrides=None,
        regime_filter=candidate.get("regime_filter"),
        **BACKTEST_KWARGS,
    )


def _run_group(service, configs: list[StrategyBacktestConfig]) -> list[dict[str, Any]]:
    prepared = service.prepare_matrix_optimization(configs)
    rows: list[dict[str, Any]] = []
    for config in configs:
        stats, error, status = _result_stats(
            service.run(config, prepared=prepared, result_policy=TRIAL_POLICY)
        )
        row: dict[str, Any] = {"status": status}
        if error is not None:
            row["error"] = error
        else:
            row["stats"] = stats
        rows.append(row)
    return rows


def _run(service, symbols: list[str]) -> dict[str, Any]:
    folds = generate_folds(START, END, TRAIN_DAYS, TEST_DAYS, STEP_DAYS)
    default_candidate = next(
        candidate for candidate in BREADTH_CANDIDATES
        if candidate["id"] == DEFAULT_CANDIDATE_ID
    )
    fold_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for fold in folds:
        train_results = _run_group(
            service,
            [
                _config(symbols, fold.train_start, fold.train_end, candidate)
                for candidate in BREADTH_CANDIDATES
            ],
        )
        training_rows: list[dict[str, Any]] = []
        for candidate, result in zip(BREADTH_CANDIDATES, train_results, strict=True):
            row = {"candidate": dict(candidate), "status": result["status"]}
            if "error" in result:
                row["error"] = result["error"]
                failures.append({
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
        selected_test, default_test = _run_group(
            service,
            [
                _config(symbols, fold.test_start, fold.test_end, selected_candidate),
                _config(symbols, fold.test_start, fold.test_end, default_candidate),
            ],
        )
        for role, candidate, result in (
            ("selected", selected_candidate, selected_test),
            ("default", default_candidate, default_test),
        ):
            if "error" in result:
                failures.append({
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
        "training_evaluation_budget": len(BREADTH_CANDIDATES) * len(folds),
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
    if resume and OUT.exists():
        existing = json.loads(OUT.read_text(encoding="utf-8"))
        if existing.get("protocol_hash") != digest:
            raise ValueError("existing checkpoint protocol does not match current config")
        if existing.get("status") == "complete":
            print(f"[bullish-breadth] skip complete -> {OUT}", flush=True)
            return existing

    store, service = _make_service()
    try:
        result = _run(service, symbols)
    finally:
        store.db.close()
    comparison = result["comparison"]
    payload = {
        "protocol_hash": digest,
        "protocol": protocol,
        "strategy": result,
        "status": "complete",
        "elapsed_seconds": round(time.time() - started, 1),
        "promotion_summary": comparison["status"],
        "note": "Historical replay only. Default production behavior remains unchanged.",
    }
    _atomic_write(payload)
    print(
        f"[bullish-breadth] {comparison['status']} "
        f"selected={comparison['optimized']['compounded_return']:+.2%} "
        f"default={comparison['default']['compounded_return']:+.2%} -> {OUT}",
        flush=True,
    )
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    main(resume=args.resume)
