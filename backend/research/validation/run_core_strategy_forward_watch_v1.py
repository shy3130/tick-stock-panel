"""Frozen forward watch for the two surviving core-strategy hypotheses.

The protocol ends calibration on 2026-06-30 and observes only data from
2026-07-01 onward.  Re-running extends the same observation window; it never
changes candidate definitions or promotes automatically.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from app.backtest.strategy import StrategyBacktestConfig
from research.common.universe import universe_manifest
from research.optimization.run_core_portfolio_walkforward_v1 import (
    CANDIDATES,
    DEFAULT_CANDIDATE_ID,
    _make_service,
    _result_stats,
    select_training_candidate,
    training_score,
)
from research.optimization.run_core_strategy_walkforward_v1 import (
    BACKTEST_KWARGS,
    N_SYMBOLS,
    SEED,
)
from research.optimization.run_core_strategy_walkforward_v1 import (
    END as CALIBRATION_END,
)
from research.paths import (
    DATA_DIR,
    OPTIMIZATION_ARTIFACTS_DIR,
    VALIDATION_ARTIFACTS_DIR,
    ensure_artifact_dirs,
)

OUT = VALIDATION_ARTIFACTS_DIR / "core_strategy_forward_watch_v1.json"
PARAM_SOURCE = OPTIMIZATION_ARTIFACTS_DIR / "core_strategy_walkforward_v1.json"
PORTFOLIO_SOURCE = OPTIMIZATION_ARTIFACTS_DIR / "core_portfolio_walkforward_v1.json"
CALIBRATION_START = date(2026, 1, 2)
OBSERVATION_START = date(2026, 7, 1)
MINIMUM_TRADING_DAYS = 60
TARGET_TRADING_DAYS = 120

OVERSOLD_PARAMS: dict[str, Any] = {
    "min_change": 2.0,
    "require_above_ma5": True,
    "rsi_max": 40.0,
    "use_change_filter": True,
    "use_rsi_filter": True,
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _validate_sources(
    parameter_source: dict[str, Any],
    portfolio_source: dict[str, Any],
) -> list[str]:
    if parameter_source.get("status") != "complete":
        raise ValueError("parameter source is not complete")
    if portfolio_source.get("status") != "complete":
        raise ValueError("portfolio source is not complete")
    frozen = parameter_source["strategies"]["oversold_reversal"]["next_frozen_params"]
    if frozen.get("params") != OVERSOLD_PARAMS:
        raise ValueError("oversold frozen params differ from the registered watch candidate")
    param_manifest = parameter_source["protocol"]["universe_manifest"]
    portfolio_manifest = portfolio_source["protocol"]["universe_manifest"]
    if param_manifest.get("symbols_sha256") != portfolio_manifest.get("symbols_sha256"):
        raise ValueError("source artifacts use different universes")
    symbols = list(param_manifest.get("symbols") or [])
    if len(symbols) != N_SYMBOLS:
        raise ValueError("source universe is incomplete")
    return symbols


def frozen_protocol(
    symbols: list[str],
    parameter_source: dict[str, Any],
    portfolio_source: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": 1,
        "status": "frozen_forward_watch",
        "calibration": [str(CALIBRATION_START), str(CALIBRATION_END)],
        "observation_start": str(OBSERVATION_START),
        "seed": SEED,
        "universe_manifest": universe_manifest(
            symbols,
            seed=SEED,
            requested_size=N_SYMBOLS,
            start=date(2024, 9, 24),
            end=CALIBRATION_END,
        ),
        "source_protocols": {
            "parameter_search": parameter_source["protocol_hash"],
            "portfolio_search": portfolio_source["protocol_hash"],
        },
        "source_files": {
            "parameter_search": PARAM_SOURCE.name,
            "portfolio_search": PORTFOLIO_SOURCE.name,
        },
        "hypotheses": {
            "oversold_reversal": {
                "candidate_params": OVERSOLD_PARAMS,
                "baseline_params": None,
                "portfolio": {
                    "max_positions": 10,
                    "position_sizing": "equal",
                },
                "selection": "modal train-only parameter winner from seven historical folds",
            },
            "trend_breakout": {
                "candidate_params": None,
                "baseline_portfolio_id": DEFAULT_CANDIDATE_ID,
                "portfolio_candidates": CANDIDATES,
                "selection": (
                    "one training-only choice on the fixed calibration interval using "
                    "total_return - 0.5 * abs(max_drawdown)"
                ),
            },
        },
        "fresh_data_boundary": (
            "No observation date may be on or before 2026-06-30. Observation metrics "
            "cannot change candidates, calibration, or thresholds."
        ),
        "readiness": {
            "minimum_trading_days": MINIMUM_TRADING_DAYS,
            "target_trading_days": TARGET_TRADING_DAYS,
            "auto_promote": False,
        },
        "backtest_kwargs": BACKTEST_KWARGS,
    }


def readiness_gate(observed_days: int, comparisons: dict[str, Any]) -> dict[str, Any]:
    ready = observed_days >= MINIMUM_TRADING_DAYS
    return {
        "status": "READY_FOR_FROZEN_REVIEW" if ready else "PENDING_DATA",
        "observed_fresh_trading_days": observed_days,
        "minimum_fresh_trading_days": MINIMUM_TRADING_DAYS,
        "target_fresh_trading_days": TARGET_TRADING_DAYS,
        "remaining_to_minimum": max(0, MINIMUM_TRADING_DAYS - observed_days),
        "metrics_locked_until_ready": not ready,
        "auto_promote": False,
        "comparison_snapshot": comparisons,
        "note": (
            "READY_FOR_FROZEN_REVIEW permits one human/audited review only; it does not "
            "promote either candidate automatically."
        ),
    }


def pair_comparison(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    candidate_stats = candidate.get("stats")
    baseline_stats = baseline.get("stats")
    if candidate_stats is None or baseline_stats is None:
        return {
            "status": "INCOMPLETE",
            "candidate_error": candidate.get("error"),
            "baseline_error": baseline.get("error"),
        }
    delta = candidate_stats["total_return"] - baseline_stats["total_return"]
    return {
        "status": "OBSERVED_NOT_PROMOTED",
        "candidate_total_return": candidate_stats["total_return"],
        "baseline_total_return": baseline_stats["total_return"],
        "return_delta": round(delta, 4),
        "candidate_max_drawdown": candidate_stats["max_drawdown"],
        "baseline_max_drawdown": baseline_stats["max_drawdown"],
        "candidate_trades": candidate_stats["n_trades"],
        "baseline_trades": baseline_stats["n_trades"],
        "snapshot_conditions": {
            "candidate_positive": candidate_stats["total_return"] > 0,
            "beats_baseline": delta > 0,
            "drawdown_within_3pp": (
                candidate_stats["max_drawdown"] >= baseline_stats["max_drawdown"] - 0.03
            ),
        },
    }


def _available_observation_dates() -> list[date]:
    return sorted(
        value
        for value in (
            pl.scan_parquet(
                str(DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet"),
                hive_partitioning=True,
            )
            .filter(pl.col("date") >= OBSERVATION_START)
            .select("date")
            .unique()
            .collect()["date"]
            .to_list()
        )
        if value >= OBSERVATION_START
    )


def _base_config(
    strategy_id: str,
    symbols: list[str],
    start: date,
    end: date,
    *,
    params: dict[str, Any] | None,
    max_positions: int,
    position_sizing: str,
) -> StrategyBacktestConfig:
    kwargs = dict(BACKTEST_KWARGS)
    kwargs["max_positions"] = max_positions
    kwargs["position_sizing"] = position_sizing
    return StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=symbols,
        start=start,
        end=end,
        params=params,
        overrides=None,
        **kwargs,
    )


def _run_group(service, configs: list[StrategyBacktestConfig]) -> list[dict[str, Any]]:
    prepared = service.prepare_matrix_optimization(configs)
    rows: list[dict[str, Any]] = []
    from research.optimization.run_core_portfolio_walkforward_v1 import TRIAL_POLICY

    for config in configs:
        stats, error, status = _result_stats(
            service.run(config, prepared=prepared, result_policy=TRIAL_POLICY)
        )
        row: dict[str, Any] = {"status": status, "config": {
            "strategy_id": config.strategy_id,
            "params": config.params,
            "max_positions": config.max_positions,
            "position_sizing": config.position_sizing,
        }}
        if error is not None:
            row["error"] = error
        else:
            row["stats"] = stats
        rows.append(row)
    return rows


def _calibrate_trend(service, symbols: list[str]) -> dict[str, Any]:
    configs = [
        _base_config(
            "trend_breakout",
            symbols,
            CALIBRATION_START,
            CALIBRATION_END,
            params=None,
            max_positions=int(candidate["max_positions"]),
            position_sizing=str(candidate["position_sizing"]),
        )
        for candidate in CANDIDATES
    ]
    results = _run_group(service, configs)
    rows: list[dict[str, Any]] = []
    for candidate, result in zip(CANDIDATES, results, strict=True):
        row = {"candidate": dict(candidate), "status": result["status"]}
        if "error" in result:
            row["error"] = result["error"]
        else:
            stats = result["stats"]
            score = training_score(stats)
            row["stats"] = stats
            row["training_score"] = score if math.isfinite(score) else None
            if not math.isfinite(score):
                row["ineligible_reason"] = "insufficient trades or non-finite metrics"
        rows.append(row)
    selected = select_training_candidate(rows)
    return {
        "range": [str(CALIBRATION_START), str(CALIBRATION_END)],
        "trials": rows,
        "selected_candidate": selected["candidate"],
        "selected_training": {
            key: selected[key]
            for key in ("status", "stats", "training_score", "selection_fallback")
            if key in selected
        },
    }


def main() -> dict[str, Any]:
    parameter_source = _load_json(PARAM_SOURCE)
    portfolio_source = _load_json(PORTFOLIO_SOURCE)
    symbols = _validate_sources(parameter_source, portfolio_source)
    protocol = frozen_protocol(symbols, parameter_source, portfolio_source)
    digest = _protocol_hash(protocol)
    if OUT.exists():
        existing = _load_json(OUT)
        if existing.get("protocol_hash") != digest:
            raise ValueError("existing forward watch uses a different frozen protocol")

    dates = _available_observation_dates()
    if not dates:
        payload = {
            "protocol_hash": digest,
            "protocol": protocol,
            "status": "PENDING_DATA",
            "observation": None,
            "readiness_gate": readiness_gate(0, {}),
        }
        _atomic_write(payload)
        return payload

    store, service = _make_service()
    try:
        calibration = _calibrate_trend(service, symbols)
        trend_candidate = calibration["selected_candidate"]
        trend_default = next(
            candidate for candidate in CANDIDATES if candidate["id"] == DEFAULT_CANDIDATE_ID
        )
        oversold_configs = [
            _base_config(
                "oversold_reversal", symbols, dates[0], dates[-1],
                params=OVERSOLD_PARAMS, max_positions=10, position_sizing="equal",
            ),
            _base_config(
                "oversold_reversal", symbols, dates[0], dates[-1],
                params=None, max_positions=10, position_sizing="equal",
            ),
        ]
        trend_configs = [
            _base_config(
                "trend_breakout", symbols, dates[0], dates[-1], params=None,
                max_positions=int(trend_candidate["max_positions"]),
                position_sizing=str(trend_candidate["position_sizing"]),
            ),
            _base_config(
                "trend_breakout", symbols, dates[0], dates[-1], params=None,
                max_positions=int(trend_default["max_positions"]),
                position_sizing=str(trend_default["position_sizing"]),
            ),
        ]
        oversold_candidate, oversold_baseline = _run_group(service, oversold_configs)
        trend_selected, trend_baseline = _run_group(service, trend_configs)
    finally:
        store.db.close()

    results = {
        "oversold_reversal": {
            "candidate": oversold_candidate,
            "baseline": oversold_baseline,
            "comparison": pair_comparison(oversold_candidate, oversold_baseline),
        },
        "trend_breakout": {
            "calibration": calibration,
            "candidate": trend_selected,
            "baseline": trend_baseline,
            "comparison": pair_comparison(trend_selected, trend_baseline),
        },
    }
    comparisons = {key: value["comparison"] for key, value in results.items()}
    gate = readiness_gate(len(dates), comparisons)
    payload = {
        "protocol_hash": digest,
        "protocol": protocol,
        "source_file_sha256": {
            PARAM_SOURCE.name: _sha256(PARAM_SOURCE),
            PORTFOLIO_SOURCE.name: _sha256(PORTFOLIO_SOURCE),
        },
        "status": gate["status"],
        "observation": {
            "start": str(dates[0]),
            "end": str(dates[-1]),
            "trading_days": len(dates),
            "results": results,
        },
        "readiness_gate": gate,
        "failures": [
            {"hypothesis": key, "role": role, "error": row.get("error")}
            for key, result in results.items()
            for role, row in (("candidate", result["candidate"]), ("baseline", result["baseline"]))
            if row.get("error")
        ],
    }
    _atomic_write(payload)
    print(
        f"[core-forward] {gate['status']} days={len(dates)} "
        f"remaining={gate['remaining_to_minimum']} -> {OUT}",
        flush=True,
    )
    return payload


if __name__ == "__main__":
    main()
