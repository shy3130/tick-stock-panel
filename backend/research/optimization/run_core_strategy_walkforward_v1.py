"""Train-only walk-forward parameter optimization for the five core strategies.

The historical folds are a deterministic replay, not fresh OOS.  Per-fold test
metrics never select parameters; they only evaluate the parameters chosen on the
immediately preceding training window.  No production overrides are written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from datetime import date
from statistics import mean, median
from typing import Any

import polars as pl

from app.backtest.optimizer import count_combinations
from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.walkforward import WalkForwardConfig, generate_folds
from app.backtest.worker import make_worker_task, run_worker_task
from app.config import settings
from app.strategy.engine import StrategyEngine
from research.common.universe import stable_symbol_sample, universe_manifest
from research.paths import DATA_DIR, OPTIMIZATION_ARTIFACTS_DIR, ensure_artifact_dirs


OUT = OPTIMIZATION_ARTIFACTS_DIR / "core_strategy_walkforward_v1.json"
START = date(2024, 9, 24)
END = date(2026, 6, 30)
N_SYMBOLS = 400
SEED = 20260723
TRAIN_DAYS = 180
TEST_DAYS = 60
STEP_DAYS = 60
OBJECTIVE = "calmar"
V1_STRATEGY_IDS = frozenset({
    "bullish_alignment",
    "trend_breakout",
    "pullback_to_support",
    "oversold_reversal",
    "limit_up_momentum",
})

BACKTEST_KWARGS: dict[str, Any] = {
    "matching": "open_t+1",
    "fees_pct": 0.0002,
    "slippage_bps": 5.0,
    "max_positions": 10,
    "max_exposure_pct": 1.0,
    "initial_capital": 1_000_000.0,
    "position_sizing": "equal",
    "mode": "position",
    "holding_days": 5,
    "asset_type": "stock",
    "minute_fill": False,
}

SPECS: dict[str, dict[str, Any]] = {
    "bullish_alignment": {
        "base_params": {"require_ma_alignment": True},
        "param_grid": {"require_positive_momentum": [True, False]},
    },
    "trend_breakout": {
        "base_params": {
            "require_above_ma60": True,
            "require_n_day_high": True,
            "use_volume_filter": True,
        },
        "param_grid": {"vol_ratio_min": [1.0, 1.5, 2.0, 2.5, 3.0]},
    },
    "pullback_to_support": {
        "base_params": {
            "use_ma20_proximity": True,
            "use_volume_filter": True,
            "require_above_ma60": True,
        },
        "param_grid": {
            "ma_proximity": [0.015, 0.02, 0.03],
            "vol_ratio_max": [0.6, 0.8],
            "require_positive_momentum": [True, False],
        },
    },
    "oversold_reversal": {
        "base_params": {
            "use_rsi_filter": True,
            "use_change_filter": True,
            "require_above_ma5": True,
        },
        "param_grid": {
            "rsi_max": [20.0, 30.0, 40.0],
            "min_change": [0.5, 1.0, 2.0],
        },
    },
    "limit_up_momentum": {
        "base_params": {
            "use_change_filter": True,
            "use_boards_filter": True,
        },
        "param_grid": {
            "min_change": [3.0, 5.0, 7.0],
            "min_boards": [1, 2, 3],
        },
    },
}


def select_universe() -> list[str]:
    symbols = (
        pl.scan_parquet(
            str(DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet"),
            hive_partitioning=True,
        )
        .filter((pl.col("date") >= START) & (pl.col("date") <= END))
        .select("symbol")
        .unique()
        .collect()["symbol"]
        .to_list()
    )
    return stable_symbol_sample(symbols, N_SYMBOLS, SEED)


def _strategy_engine() -> StrategyEngine:
    from app.backtest.worker import _strategy_dirs

    return StrategyEngine(strategy_dirs=_strategy_dirs(settings.data_dir))


def validate_specs(engine: StrategyEngine) -> dict[str, int]:
    if set(SPECS) != set(V1_STRATEGY_IDS):
        raise ValueError("v1 optimization specs must cover its frozen five-strategy protocol")
    counts: dict[str, int] = {}
    for strategy_id, spec in SPECS.items():
        strategy = engine.get(strategy_id)
        counts[strategy_id] = count_combinations(
            strategy.meta.get("params", []),
            spec["param_grid"],
        )
    return counts


def canonical_protocol(symbols: list[str], combination_counts: dict[str, int]) -> dict[str, Any]:
    folds = generate_folds(START, END, TRAIN_DAYS, TEST_DAYS, STEP_DAYS)
    return {
        "version": 2,
        "evidence_status": "canonical_historical_replay_not_fresh_oos",
        "selection_boundary": (
            "Each fold selects params using training metrics only. Test metrics are evaluation-only."
        ),
        "seed": SEED,
        "universe_manifest": universe_manifest(
            symbols,
            seed=SEED,
            requested_size=N_SYMBOLS,
            start=START,
            end=END,
        ),
        "range": [str(START), str(END)],
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
        "objective": OBJECTIVE,
        "no_signal_policy": (
            "A successful no-trade default fold is recorded explicitly as cash with zero return; "
            "other execution errors remain failures."
        ),
        "backtest_kwargs": BACKTEST_KWARGS,
        "specs": SPECS,
        "combination_counts": combination_counts,
        "promotion_rule": {
            "valid_fold_ratio": 1.0,
            "positive_optimized_fold_ratio_min": 0.6,
            "beat_default_fold_ratio_min": 0.6,
            "mean_return_delta_min": 0.0,
            "compounded_return_delta_min": 0.0,
            "mean_drawdown_tolerance": 0.03,
            "minimum_total_oos_trades": 50,
            "result": "candidate_for_future_frozen_oos_only",
        },
    }


def protocol_hash(protocol: dict[str, Any]) -> str:
    payload = json.dumps(protocol, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _atomic_write(payload: dict[str, Any]) -> None:
    ensure_artifact_dirs()
    temporary = OUT.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(OUT)


def _metrics(result: dict[str, Any]) -> dict[str, Any]:
    stats = result.get("stats") or {}
    return {
        "total_return": float(stats.get("total_return", 0.0)),
        "max_drawdown": float(stats.get("max_drawdown", 0.0)),
        "sharpe": float(stats.get("sharpe", 0.0)),
        "sortino": float(stats.get("sortino", 0.0)),
        "calmar": float(stats.get("calmar", 0.0)),
        "win_rate": float(stats.get("win_rate", 0.0)),
        "n_trades": int(stats.get("n_trades", 0)),
    }


def _run_default_fold(strategy_id: str, symbols: list[str], fold) -> dict[str, Any]:
    cfg = StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=symbols,
        start=fold.test_start,
        end=fold.test_end,
        params=None,
        overrides=None,
        **BACKTEST_KWARGS,
    )
    result = run_worker_task(make_worker_task("backtest", settings.data_dir, cfg))
    return default_fold_record(fold.index, result)


def default_fold_record(index: int, result: dict[str, Any]) -> dict[str, Any]:
    """Distinguish a valid cash/no-signal fold from an actual execution failure."""
    if result.get("error"):
        message = str(result["error"])
        if "未产生买入信号" in message:
            return {
                "index": index,
                "status": "no_signal_cash",
                "message": message,
                "stats": {
                    "total_return": 0.0,
                    "max_drawdown": 0.0,
                    "sharpe": 0.0,
                    "sortino": 0.0,
                    "calmar": 0.0,
                    "win_rate": 0.0,
                    "n_trades": 0,
                },
            }
        return {"index": index, "error": message}
    return {"index": index, "stats": _metrics(result)}


def aggregate_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    stats = [row["stats"] for row in rows if "stats" in row]
    if not stats:
        return {
            "n_folds": 0,
            "compounded_return": 0.0,
            "positive_fold_ratio": 0.0,
            "mean_total_return": None,
            "median_total_return": None,
            "mean_max_drawdown": None,
            "mean_sharpe": None,
            "total_trades": 0,
        }
    equity = 1.0
    for item in stats:
        equity *= 1.0 + item["total_return"]
    returns = [item["total_return"] for item in stats]
    return {
        "n_folds": len(stats),
        "compounded_return": round(equity - 1.0, 4),
        "positive_fold_ratio": round(sum(value > 0 for value in returns) / len(returns), 4),
        "mean_total_return": round(mean(returns), 4),
        "median_total_return": round(median(returns), 4),
        "mean_max_drawdown": round(mean(item["max_drawdown"] for item in stats), 4),
        "mean_sharpe": round(mean(item["sharpe"] for item in stats), 3),
        "total_trades": sum(item["n_trades"] for item in stats),
    }


def next_frozen_params(folds: list[dict[str, Any]], base_params: dict[str, Any]) -> dict[str, Any] | None:
    """Use training-selected params only; OOS values cannot affect the result."""
    encoded = [
        json.dumps({**base_params, **fold["best_params"]}, sort_keys=True)
        for fold in folds
        if fold.get("best_params") is not None
    ]
    if not encoded:
        return None
    counts = Counter(encoded)
    selected, frequency = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    return {
        "params": json.loads(selected),
        "selected_train_folds": frequency,
        "selection_share": round(frequency / len(encoded), 4),
        "selection_method": "mode of per-fold train-only winners; lexical tie-break",
    }


def compare_to_default(
    optimized_folds: list[dict[str, Any]],
    default_folds: list[dict[str, Any]],
    planned_folds: int,
) -> dict[str, Any]:
    default_by_index = {row["index"]: row for row in default_folds if "stats" in row}
    comparisons: list[dict[str, Any]] = []
    optimized_rows: list[dict[str, Any]] = []
    paired_defaults: list[dict[str, Any]] = []
    for fold in optimized_folds:
        default = default_by_index.get(fold["index"])
        if default is None:
            continue
        optimized = {
            "index": fold["index"],
            "stats": {
                key: fold["oos_stats"][key]
                for key in (
                    "total_return", "max_drawdown", "sharpe", "sortino", "calmar",
                    "win_rate", "n_trades",
                )
            },
        }
        optimized_rows.append(optimized)
        paired_defaults.append(default)
        comparisons.append({
            "index": fold["index"],
            "best_params": fold["best_params"],
            "optimized": optimized["stats"],
            "default": default["stats"],
            "return_delta": round(
                optimized["stats"]["total_return"] - default["stats"]["total_return"], 4
            ),
        })

    optimized_agg = aggregate_stats(optimized_rows)
    default_agg = aggregate_stats(paired_defaults)
    n_pairs = len(comparisons)
    beat_ratio = (
        sum(row["return_delta"] > 0 for row in comparisons) / n_pairs if n_pairs else 0.0
    )
    mean_delta = (
        mean(row["return_delta"] for row in comparisons) if comparisons else float("-inf")
    )
    conditions = {
        "all_folds_valid": n_pairs == planned_folds,
        "positive_fold_ratio": optimized_agg["positive_fold_ratio"] >= 0.6,
        "beat_default_fold_ratio": beat_ratio >= 0.6,
        "mean_return_delta": mean_delta > 0.0,
        "compounded_return_delta": (
            optimized_agg["compounded_return"] > default_agg["compounded_return"]
        ),
        "drawdown_within_tolerance": (
            optimized_agg["mean_max_drawdown"] is not None
            and default_agg["mean_max_drawdown"] is not None
            and optimized_agg["mean_max_drawdown"]
            >= default_agg["mean_max_drawdown"] - 0.03
        ),
        "minimum_total_oos_trades": optimized_agg["total_trades"] >= 50,
    }
    return {
        "comparisons": comparisons,
        "optimized": optimized_agg,
        "default": default_agg,
        "beat_default_fold_ratio": round(beat_ratio, 4),
        "mean_return_delta": round(mean_delta, 4) if comparisons else None,
        "conditions": conditions,
        "status": (
            "CANDIDATE_FOR_FUTURE_FROZEN_OOS"
            if all(conditions.values())
            else "REJECTED_HISTORICAL_REPLAY"
        ),
        "auto_apply": False,
    }


def main(*, resume: bool = False) -> dict[str, Any]:
    started = time.time()
    symbols = select_universe()
    engine = _strategy_engine()
    combination_counts = validate_specs(engine)
    protocol = canonical_protocol(symbols, combination_counts)
    digest = protocol_hash(protocol)
    records: dict[str, Any] = {}
    previous_elapsed_seconds: float | None = None
    if resume and OUT.exists():
        existing = json.loads(OUT.read_text(encoding="utf-8"))
        if existing.get("protocol_hash") != digest:
            raise ValueError("existing checkpoint protocol does not match current config")
        records = dict(existing.get("strategies") or {})
        if existing.get("status") == "complete":
            previous_elapsed_seconds = existing.get("elapsed_seconds")

    folds = generate_folds(START, END, TRAIN_DAYS, TEST_DAYS, STEP_DAYS)
    evaluated_count = 0
    print(
        f"[core-opt] universe={len(symbols)} folds={len(folds)} "
        f"strategies={len(SPECS)} objective={OBJECTIVE} resume={resume}",
        flush=True,
    )
    for strategy_id, spec in SPECS.items():
        if strategy_id in records:
            print(f"[core-opt] skip {strategy_id}", flush=True)
            continue
        evaluated_count += 1
        print(
            f"\n[core-opt] {strategy_id} combinations={combination_counts[strategy_id]}",
            flush=True,
        )
        wf_cfg = WalkForwardConfig(
            strategy_id=strategy_id,
            symbols=symbols,
            start=START,
            end=END,
            param_grid=spec["param_grid"],
            objective=OBJECTIVE,
            train_days=TRAIN_DAYS,
            test_days=TEST_DAYS,
            step_days=STEP_DAYS,
            max_workers=4,
            base_params=spec["base_params"],
            overrides=None,
            backtest_kwargs=dict(BACKTEST_KWARGS),
        )
        try:
            wf_result = run_worker_task(
                make_worker_task("walkforward", settings.data_dir, wf_cfg)
            )
            if wf_result.get("error"):
                raise RuntimeError(str(wf_result["error"]))
            default_folds = [
                _run_default_fold(strategy_id, symbols, fold)
                for fold in folds
            ]
            comparison = compare_to_default(
                wf_result.get("folds") or [],
                default_folds,
                len(folds),
            )
            records[strategy_id] = {
                "grid_combinations": combination_counts[strategy_id],
                "walkforward": wf_result,
                "default_folds": default_folds,
                "comparison": comparison,
                "next_frozen_params": next_frozen_params(
                    wf_result.get("folds") or [],
                    spec["base_params"],
                ),
            }
            print(
                f"[core-opt] {strategy_id} status={comparison['status']} "
                f"opt={comparison['optimized']['compounded_return']:+.2%} "
                f"default={comparison['default']['compounded_return']:+.2%}",
                flush=True,
            )
        except Exception as exc:
            records[strategy_id] = {
                "error": f"{type(exc).__name__}: {exc}",
                "status": "FAILED_EXPLICITLY",
            }
            print(f"[core-opt] FAIL {strategy_id}: {exc}", flush=True)

        _atomic_write({
            "protocol_hash": digest,
            "protocol": protocol,
            "strategies": records,
            "status": "running",
        })

    elapsed_seconds = round(time.time() - started, 1)
    if evaluated_count == 0 and previous_elapsed_seconds is not None:
        elapsed_seconds = previous_elapsed_seconds
    payload = {
        "protocol_hash": digest,
        "protocol": protocol,
        "strategies": records,
        "status": "complete",
        "elapsed_seconds": elapsed_seconds,
        "promotion_summary": {
            strategy_id: record.get("comparison", {}).get("status", record.get("status"))
            for strategy_id, record in records.items()
        },
        "note": (
            "No production params were changed. Candidate status only freezes a hypothesis "
            "for a genuinely future observation window."
        ),
    }
    _atomic_write(payload)
    print(f"\n[core-opt] complete -> {OUT}", flush=True)
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    main(resume=args.resume)
