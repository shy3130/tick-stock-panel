"""Train-only portfolio construction search for the five core strategies.

This is a canonical historical replay, not fresh OOS.  Entry/exit logic and
strategy parameters stay unchanged.  Every fold chooses only between seven
pre-registered position-count/sizing candidates on its training interval, then
evaluates the frozen choice once on the adjacent test interval.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from collections import Counter
from typing import Any

from app.backtest.engine import BacktestEngine
from app.backtest.strategy import (
    BacktestResultPolicy,
    StrategyBacktestConfig,
    StrategyBacktestService,
)
from app.backtest.walkforward import generate_folds
from app.config import settings
from app.strategy.engine import StrategyEngine
from app.tickflow.repository import DataStore, KlineRepository
from research.common.universe import universe_manifest
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


OUT = OPTIMIZATION_ARTIFACTS_DIR / "core_portfolio_walkforward_v1.json"
MIN_TRAIN_TRADES = 30
DEFAULT_CANDIDATE_ID = "equal_10_default"
V1_STRATEGY_IDS = frozenset({
    "bullish_alignment",
    "trend_breakout",
    "pullback_to_support",
    "oversold_reversal",
    "limit_up_momentum",
})

# All candidates share the same signals, fees, exposure and execution rules.
# Lexical candidate IDs are the deterministic final tie-break.
CANDIDATES: tuple[dict[str, Any], ...] = (
    {"id": "equal_10_default", "max_positions": 10, "position_sizing": "equal"},
    {"id": "equal_20", "max_positions": 20, "position_sizing": "equal"},
    {"id": "equal_3", "max_positions": 3, "position_sizing": "equal"},
    {"id": "equal_5", "max_positions": 5, "position_sizing": "equal"},
    {"id": "score_weight_10", "max_positions": 10, "position_sizing": "score_weight"},
    {"id": "score_weight_20", "max_positions": 20, "position_sizing": "score_weight"},
    {"id": "score_weight_5", "max_positions": 5, "position_sizing": "score_weight"},
)

TRIAL_POLICY = BacktestResultPolicy(
    required_stats=frozenset({
        "total_return", "max_drawdown", "sharpe", "sortino", "calmar",
        "win_rate", "n_trades",
    }),
    include_monte_carlo=False,
    include_curves=False,
    include_trades=False,
    include_per_symbol_stats=False,
    include_return_distribution=False,
    include_benchmark=False,
    include_strategy_info=False,
)


def validate_candidates() -> None:
    ids = [str(candidate["id"]) for candidate in CANDIDATES]
    if len(ids) != len(set(ids)):
        raise ValueError("portfolio candidate IDs must be unique")
    if DEFAULT_CANDIDATE_ID not in ids:
        raise ValueError("default portfolio candidate is missing")
    for candidate in CANDIDATES:
        if int(candidate["max_positions"]) <= 0:
            raise ValueError("max_positions must be positive")
        if candidate["position_sizing"] not in {"equal", "score_weight"}:
            raise ValueError("unsupported position_sizing")


def training_score(stats: dict[str, Any]) -> float:
    """Reward growth while charging half of absolute drawdown.

    A minimum trade count prevents a nearly empty training result from winning
    merely because its drawdown is tiny.
    """
    if int(stats.get("n_trades", 0)) < MIN_TRAIN_TRADES:
        return float("-inf")
    try:
        total_return = float(stats["total_return"])
        max_drawdown = float(stats["max_drawdown"])
    except (KeyError, TypeError, ValueError):
        return float("-inf")
    if not (math.isfinite(total_return) and math.isfinite(max_drawdown)):
        return float("-inf")
    return total_return - 0.5 * abs(max_drawdown)


def select_training_candidate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [
        row
        for row in rows
        if isinstance(row.get("training_score"), (int, float))
        and math.isfinite(float(row["training_score"]))
    ]
    if not eligible:
        defaults = [row for row in rows if row.get("candidate", {}).get("id") == DEFAULT_CANDIDATE_ID]
        if not defaults:
            raise ValueError("training fold has no eligible candidate and no explicit default")
        selected = defaults[0]
        return {**selected, "selection_fallback": "default_no_eligible_candidate"}
    return sorted(
        eligible,
        key=lambda row: (
            -float(row["training_score"]),
            -float(row["stats"]["total_return"]),
            -float(row["stats"]["sharpe"]),
            str(row["candidate"]["id"]),
        ),
    )[0]


def next_frozen_candidate(folds: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select the modal train winner without reading any OOS field."""
    ids = [
        str(fold["selected_candidate"]["id"])
        for fold in folds
        if fold.get("selected_candidate") is not None
    ]
    if not ids:
        return None
    counts = Counter(ids)
    selected_id, frequency = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    candidate = next(item for item in CANDIDATES if item["id"] == selected_id)
    return {
        "candidate": dict(candidate),
        "selected_train_folds": frequency,
        "selection_share": round(frequency / len(ids), 4),
        "selection_method": "mode of per-fold train-only winners; lexical tie-break",
    }


def _protocol(symbols: list[str]) -> dict[str, Any]:
    folds = generate_folds(START, END, TRAIN_DAYS, TEST_DAYS, STEP_DAYS)
    return {
        "version": 2,
        "evidence_status": "canonical_historical_replay_not_fresh_oos",
        "scope": "portfolio construction only; strategy params and signals unchanged",
        "selection_boundary": "training metrics select; adjacent test metrics evaluate only",
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
            "minimum_training_trades": MIN_TRAIN_TRADES,
            "tie_break": ["total_return", "sharpe", "candidate_id lexical"],
        },
        "candidates": CANDIDATES,
        "candidate_count_per_strategy_per_fold": len(CANDIDATES),
        "backtest_kwargs": BACKTEST_KWARGS,
        "no_signal_policy": "explicit cash: zero return, zero drawdown, zero trades",
        "promotion_rule": {
            "positive_optimized_fold_ratio_min": 0.6,
            "beat_default_fold_ratio_min": 0.6,
            "mean_return_delta_min": 0.0,
            "compounded_return_delta_min": 0.0,
            "mean_drawdown_tolerance": 0.03,
            "minimum_total_oos_trades": 50,
            "result": "future frozen candidate only; never auto-apply historical replay",
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


def _make_service() -> tuple[DataStore, StrategyBacktestService]:
    from app.backtest.worker import _strategy_dirs

    store = DataStore(settings.data_dir)
    repo = KlineRepository(store)
    strategy_engine = StrategyEngine(strategy_dirs=_strategy_dirs(settings.data_dir))
    return store, StrategyBacktestService(BacktestEngine(repo), strategy_engine)


def _config(
    strategy_id: str,
    symbols: list[str],
    start,
    end,
    candidate: dict[str, Any],
) -> StrategyBacktestConfig:
    kwargs = dict(BACKTEST_KWARGS)
    kwargs["max_positions"] = int(candidate["max_positions"])
    kwargs["position_sizing"] = str(candidate["position_sizing"])
    return StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=symbols,
        start=start,
        end=end,
        params=None,
        overrides=None,
        **kwargs,
    )


def _result_stats(result) -> tuple[dict[str, Any] | None, str | None, str]:
    if result.error:
        message = str(result.error)
        if "未产生买入信号" in message:
            return ({
                "total_return": 0.0,
                "max_drawdown": 0.0,
                "sharpe": 0.0,
                "sortino": 0.0,
                "calmar": 0.0,
                "win_rate": 0.0,
                "n_trades": 0,
            }, None, "no_signal_cash")
        return None, message, "error"
    stats = result.stats or {}
    return ({
        "total_return": float(stats.get("total_return", 0.0)),
        "max_drawdown": float(stats.get("max_drawdown", 0.0)),
        "sharpe": float(stats.get("sharpe", 0.0)),
        "sortino": float(stats.get("sortino", 0.0)),
        "calmar": float(stats.get("calmar", 0.0)),
        "win_rate": float(stats.get("win_rate", 0.0)),
        "n_trades": int(stats.get("n_trades", 0)),
    }, None, "ok")


def _run_configs(
    service: StrategyBacktestService,
    configs: list[StrategyBacktestConfig],
) -> list[tuple[dict[str, Any] | None, str | None, str]]:
    prepared = service.prepare_matrix_optimization(configs)
    return [
        _result_stats(service.run(config, prepared=prepared, result_policy=TRIAL_POLICY))
        for config in configs
    ]


def _run_strategy(
    service: StrategyBacktestService,
    strategy_id: str,
    symbols: list[str],
) -> dict[str, Any]:
    folds = generate_folds(START, END, TRAIN_DAYS, TEST_DAYS, STEP_DAYS)
    fold_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    default_candidate = next(item for item in CANDIDATES if item["id"] == DEFAULT_CANDIDATE_ID)

    for fold in folds:
        train_configs = [
            _config(strategy_id, symbols, fold.train_start, fold.train_end, candidate)
            for candidate in CANDIDATES
        ]
        training_results = _run_configs(service, train_configs)
        training_rows: list[dict[str, Any]] = []
        for candidate, (stats, error, status) in zip(CANDIDATES, training_results, strict=True):
            row = {"candidate": dict(candidate), "status": status}
            if error is not None:
                row["error"] = error
                failures.append({
                    "strategy_id": strategy_id,
                    "fold": fold.index,
                    "phase": "train",
                    "candidate_id": candidate["id"],
                    "error": error,
                })
            else:
                row["stats"] = stats
                score = training_score(stats or {})
                if math.isfinite(score):
                    row["training_score"] = score
                else:
                    row["training_score"] = None
                    row["ineligible_reason"] = (
                        f"training trades below minimum {MIN_TRAIN_TRADES} or non-finite metrics"
                    )
            training_rows.append(row)

        selected = select_training_candidate(training_rows)
        selected_candidate = selected["candidate"]
        test_candidates = [selected_candidate, default_candidate]
        test_configs = [
            _config(strategy_id, symbols, fold.test_start, fold.test_end, candidate)
            for candidate in test_candidates
        ]
        test_results = _run_configs(service, test_configs)
        test_rows = []
        for role, candidate, (stats, error, status) in zip(
            ("selected", "default"), test_candidates, test_results, strict=True
        ):
            row = {"role": role, "candidate": dict(candidate), "status": status}
            if error is not None:
                row["error"] = error
                failures.append({
                    "strategy_id": strategy_id,
                    "fold": fold.index,
                    "phase": "test",
                    "role": role,
                    "candidate_id": candidate["id"],
                    "error": error,
                })
            else:
                row["stats"] = stats
            test_rows.append(row)

        selected_test, default_test = test_rows
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
    return {
        "candidate_count": len(CANDIDATES),
        "training_evaluation_budget": len(CANDIDATES) * len(folds),
        "folds": fold_rows,
        "comparison": comparison,
        "next_frozen_candidate": next_frozen_candidate(fold_rows),
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

    ordered_ids = sorted(V1_STRATEGY_IDS)
    evaluated_count = 0
    store, service = _make_service()
    try:
        print(
            f"[core-portfolio] universe={len(symbols)} folds=7 strategies={len(ordered_ids)} "
            f"candidates={len(CANDIDATES)} resume={resume}",
            flush=True,
        )
        for strategy_id in ordered_ids:
            if strategy_id in strategies:
                print(f"[core-portfolio] skip {strategy_id}", flush=True)
                continue
            evaluated_count += 1
            try:
                result = _run_strategy(service, strategy_id, symbols)
                strategies[strategy_id] = result
                comparison = result["comparison"]
                print(
                    f"[core-portfolio] {strategy_id} {comparison['status']} "
                    f"selected={comparison['optimized']['compounded_return']:+.2%} "
                    f"default={comparison['default']['compounded_return']:+.2%}",
                    flush=True,
                )
            except Exception as exc:
                strategies[strategy_id] = {
                    "status": "FAILED_EXPLICITLY",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                print(f"[core-portfolio] FAIL {strategy_id}: {exc}", flush=True)
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
    print(f"[core-portfolio] complete -> {OUT}", flush=True)
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    main(resume=args.resume)
