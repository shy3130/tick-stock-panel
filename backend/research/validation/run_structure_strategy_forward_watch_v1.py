"""P14 frozen forward watch for the two P13 always-on strategies.

The protocol is registered on 2026-07-29.  Only trading days on or after
2026-07-30 count toward the 60/120-day readiness gates.  Earlier July data is
already visible to the researchers and must not be relabelled as fresh OOS.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import polars as pl

from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task
from app.config import settings
from research.paths import (
    DATA_DIR,
    REGIME_ARTIFACTS_DIR,
    VALIDATION_ARTIFACTS_DIR,
    ensure_artifact_dirs,
)
from research.regime.run_regime_ensemble import derive_metrics
from research.regime.run_structure_strategy_replay_v1 import BASE_OVERRIDES


VERSION = "structure_strategy_forward_watch_v1"
REGISTRATION_DATE = date(2026, 7, 29)
OBSERVATION_START = date(2026, 7, 30)
MINIMUM_TRADING_DAYS = 60
TARGET_TRADING_DAYS = 120
INITIAL_CAPITAL = 1_000_000.0

SOURCE = REGIME_ARTIFACTS_DIR / "structure_strategy_replay_v1.json"
STRUCTURE_CACHE = DATA_DIR / ".regime_cache" / "market_structure_v1.parquet"
OUT = VALIDATION_ARTIFACTS_DIR / f"{VERSION}.json"

FROZEN_KEYS = ("trend_always", "pullback_always")
FROZEN_EXECUTION: dict[str, Any] = {
    "params": None,
    "overrides": dict(BASE_OVERRIDES),
    "matching": "open_t+1",
    "entry_fill": None,
    "exit_fill": None,
    "fees_pct": 0.0002,
    "commission_pct": None,
    "stamp_tax_pct": None,
    "slippage_bps": 5.0,
    "max_positions": 10,
    "max_exposure_pct": 1.0,
    "initial_capital": INITIAL_CAPITAL,
    "position_sizing": "score_weight",
    "mode": "position",
    "holding_days": 20,
    "asset_type": "stock",
    "minute_fill": False,
    "regime_filter": None,
    "composition": None,
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _protocol_hash(protocol: dict[str, Any]) -> str:
    encoded = json.dumps(
        protocol,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _strict(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _strict(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strict(item) for item in value]
    return value


def _atomic_write(payload: dict[str, Any]) -> None:
    ensure_artifact_dirs()
    temporary = OUT.with_name(f".{OUT.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                _strict(payload),
                ensure_ascii=False,
                indent=2,
                default=str,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(OUT)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_source(source: dict[str, Any]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    if source.get("version") != "structure_strategy_replay_v1":
        raise ValueError("P14 source must be structure_strategy_replay_v1")
    if source.get("status") != "complete":
        raise ValueError("P14 source replay is incomplete")
    if source.get("evidence_status") != "canonical_historical_replay_not_fresh_oos":
        raise ValueError("P14 source has an unexpected evidence status")
    universe = source.get("universe") or {}
    symbols = list(universe.get("symbols") or [])
    if universe.get("seed") != 20260723 or len(symbols) != 400:
        raise ValueError("P14 requires the frozen canonical 400-symbol universe")
    if len(set(symbols)) != len(symbols):
        raise ValueError("P14 source universe contains duplicate symbols")

    configurations = {
        str(row["key"]): dict(row)
        for row in source.get("configurations") or []
        if isinstance(row, dict) and row.get("key")
    }
    expected = {
        "trend_always": "trend_breakout",
        "pullback_always": "pullback_to_support",
    }
    for key, strategy_id in expected.items():
        row = configurations.get(key)
        if row is None or row.get("strategy_id") != strategy_id:
            raise ValueError(f"P14 source is missing frozen configuration {key}")
        if row.get("regime_filter") is not None or row.get("composition") is not None:
            raise ValueError(f"P14 frozen configuration {key} must remain always-on")
    return symbols, {key: configurations[key] for key in FROZEN_KEYS}


def frozen_protocol(
    source: dict[str, Any],
    *,
    source_sha256: str,
    market_structure_protocol_hash: str,
) -> dict[str, Any]:
    symbols, configurations = _validate_source(source)
    historical = source.get("aggregate") or {}
    return {
        "version": VERSION,
        "status": "frozen_forward_watch",
        "registered_on": REGISTRATION_DATE,
        "fresh_observation_start": OBSERVATION_START,
        "calibration_and_selection_end": "2026-06-30",
        "pre_registration_data_policy": (
            "2026-07-01 through 2026-07-29 was visible before registration and "
            "cannot count toward fresh readiness."
        ),
        "universe": {
            "size": len(symbols),
            "seed": source["universe"]["seed"],
            "symbols": symbols,
        },
        "source": {
            "file": SOURCE.name,
            "sha256": source_sha256,
            "market_structure_protocol_hash": market_structure_protocol_hash,
        },
        "candidates": {
            key: {
                "historical_configuration": configurations[key],
                "execution": dict(FROZEN_EXECUTION),
                "historical_reference_only": historical.get(key),
            }
            for key in FROZEN_KEYS
        },
        "attribution": {
            "regime_source": "market_structure_v1 cache",
            "lag_rule": "label on t uses t-1 or earlier close data",
            "daily_return_rule": "consecutive close-to-close portfolio equity changes",
            "benchmark_rule": "same-date benchmark close changes",
            "turnover_rule": (
                "closed_trade_entry_value / average equity; this is an explicit "
                "one-way proxy because open-position transactions are not exposed"
            ),
        },
        "readiness": {
            "minimum_fresh_trading_days": MINIMUM_TRADING_DAYS,
            "target_fresh_trading_days": TARGET_TRADING_DAYS,
            "auto_promote": False,
            "review_only": True,
        },
        "anti_tuning": (
            "Observation metrics cannot alter universe, candidates, parameters, "
            "execution settings, structure thresholds, or readiness thresholds."
        ),
    }


def readiness_gate(observed_days: int) -> dict[str, Any]:
    ready = observed_days >= MINIMUM_TRADING_DAYS
    return {
        "status": "READY_FOR_FROZEN_REVIEW" if ready else "PENDING_DATA",
        "observed_fresh_trading_days": observed_days,
        "minimum_fresh_trading_days": MINIMUM_TRADING_DAYS,
        "target_fresh_trading_days": TARGET_TRADING_DAYS,
        "remaining_to_minimum": max(0, MINIMUM_TRADING_DAYS - observed_days),
        "remaining_to_target": max(0, TARGET_TRADING_DAYS - observed_days),
        "auto_promote": False,
        "note": (
            "Reaching the threshold unlocks an audited review only. It never changes "
            "production strategy settings automatically."
        ),
    }


def _load_structure_labels() -> tuple[dict[date, str], str]:
    if not STRUCTURE_CACHE.exists():
        raise ValueError("market structure cache is missing; regenerate it before P14")
    frame = pl.read_parquet(
        STRUCTURE_CACHE,
        columns=["date", "regime", "protocol_hash"],
    ).with_columns(pl.col("date").cast(pl.Date, strict=False))
    hashes = frame["protocol_hash"].drop_nulls().cast(pl.Utf8).unique().to_list()
    if len(hashes) != 1:
        raise ValueError("market structure cache must contain one protocol hash")
    labels = {
        row["date"]: str(row["regime"])
        for row in frame.select("date", "regime").to_dicts()
        if row["date"] is not None
    }
    return labels, hashes[0]


def _available_fresh_dates(labels: dict[date, str]) -> list[date]:
    dates = sorted(
        pl.scan_parquet(
            str(DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet"),
            hive_partitioning=True,
        )
        .filter(pl.col("date") >= OBSERVATION_START)
        .select(pl.col("date").cast(pl.Date, strict=False))
        .unique()
        .collect()["date"]
        .drop_nulls()
        .to_list()
    )
    missing_labels = [value for value in dates if value not in labels]
    if missing_labels:
        raise ValueError(
            "market structure cache is stale for fresh dates: "
            + ",".join(str(value) for value in missing_labels[:5])
        )
    return dates


def _backtest_config(strategy_id: str, symbols: list[str], start: date, end: date) -> StrategyBacktestConfig:
    return StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=symbols,
        start=start,
        end=end,
        **FROZEN_EXECUTION,
    )


def _curve_values(curve: Iterable[dict[str, Any]], key: str) -> dict[date, float]:
    values: dict[date, float] = {}
    for row in curve:
        try:
            trading_day = date.fromisoformat(str(row["date"])[:10])
            value = float(row[key])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            values[trading_day] = value
    return values


def daily_attribution(
    equity_curve: Iterable[dict[str, Any]],
    benchmark_curve: Iterable[dict[str, Any]],
    labels: dict[date, str],
) -> list[dict[str, Any]]:
    """Attribute comparable consecutive-day returns to the causal label on day t."""
    equity = _curve_values(equity_curve, "value")
    benchmark = _curve_values(benchmark_curve, "close")
    common_dates = sorted(set(equity) & set(benchmark) & set(labels))
    rows: list[dict[str, Any]] = []
    for previous, current in zip(common_dates, common_dates[1:], strict=False):
        strategy_return = equity[current] / equity[previous] - 1.0
        benchmark_return = benchmark[current] / benchmark[previous] - 1.0
        relative_return = (1.0 + strategy_return) / (1.0 + benchmark_return) - 1.0
        rows.append(
            {
                "date": current,
                "regime": labels[current],
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "relative_return": relative_return,
                "equity": equity[current],
            }
        )
    return rows


def _return_metrics(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    returns = [float(row[key]) for row in rows if math.isfinite(float(row[key]))]
    level = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        level *= 1.0 + value
        peak = max(peak, level)
        max_drawdown = min(max_drawdown, level / peak - 1.0)
    if len(returns) > 1:
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / len(returns)
        std = math.sqrt(variance)
        sharpe = mean / std * math.sqrt(252.0) if std > 0 else 0.0
    else:
        sharpe = 0.0
    return {
        "attributed_days": len(returns),
        "compounded_return": round(level - 1.0, 6),
        "max_drawdown": round(max_drawdown, 6),
        "annualized_sharpe": round(sharpe, 4),
        "positive_days": sum(value > 0 for value in returns),
    }


def regime_attribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for regime in ("structural_bull", "structural_bear"):
        selected = [row for row in rows if row["regime"] == regime]
        output[regime] = {
            "strategy": _return_metrics(selected, "strategy_return"),
            "benchmark": _return_metrics(selected, "benchmark_return"),
            "relative": _return_metrics(selected, "relative_return"),
        }
    return output


def turnover_proxy(
    trades: Iterable[dict[str, Any]],
    equity_curve: Iterable[dict[str, Any]],
    labels: dict[date, str],
    observed_days: int,
) -> dict[str, Any]:
    equity = list(_curve_values(equity_curve, "value").values())
    average_equity = sum(equity) / len(equity) if equity else INITIAL_CAPITAL
    by_regime = {"structural_bull": 0.0, "structural_bear": 0.0, "unlabelled": 0.0}
    gross_entry_value = 0.0
    closed_trades = 0
    for trade in trades:
        try:
            value = abs(float(trade.get("entry_value") or 0.0))
            entry_day = date.fromisoformat(str(trade["entry_date"])[:10])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        gross_entry_value += value
        closed_trades += 1
        regime = labels.get(entry_day, "unlabelled")
        by_regime[regime if regime in by_regime else "unlabelled"] += value
    one_way = gross_entry_value / average_equity if average_equity > 0 else 0.0
    return {
        "method": "closed_trade_entry_value_over_average_equity",
        "closed_trades_counted": closed_trades,
        "gross_entry_value": round(gross_entry_value, 2),
        "average_equity": round(average_equity, 2),
        "one_way_turnover_proxy": round(one_way, 6),
        "annualized_one_way_turnover_proxy": (
            round(one_way * 252.0 / observed_days, 6) if observed_days > 0 else 0.0
        ),
        "entry_value_by_regime": {
            key: round(value, 2) for key, value in by_regime.items()
        },
        "limitation": (
            "Counts entry value only for closed trades; open-position transactions "
            "are unavailable and therefore excluded."
        ),
    }


def _run_candidate(
    key: str,
    strategy_id: str,
    symbols: list[str],
    start: date,
    end: date,
    labels: dict[date, str],
    observed_days: int,
) -> dict[str, Any]:
    config = _backtest_config(strategy_id, symbols, start, end)
    result = run_worker_task(make_worker_task("backtest", settings.data_dir, config))
    if result.get("error"):
        return {"key": key, "status": "error", "error": str(result["error"])}
    daily = daily_attribution(
        result.get("equity_curve") or [],
        result.get("benchmark_curve") or [],
        labels,
    )
    overall = {
        "engine": derive_metrics(result),
        "curve_strategy": _return_metrics(daily, "strategy_return"),
        "curve_benchmark": _return_metrics(daily, "benchmark_return"),
        "curve_relative": _return_metrics(daily, "relative_return"),
        "average_exposure": round(
            sum(float(row.get("exposure") or 0.0) for row in result.get("equity_curve") or [])
            / max(len(result.get("equity_curve") or []), 1),
            6,
        ),
    }
    return {
        "key": key,
        "status": "observed_not_promoted",
        "strategy_id": strategy_id,
        "execution": dict(FROZEN_EXECUTION),
        "overall": overall,
        "by_regime": regime_attribution(daily),
        "turnover": turnover_proxy(
            result.get("trades") or [],
            result.get("equity_curve") or [],
            labels,
            observed_days,
        ),
        "daily": daily,
    }


def main() -> dict[str, Any]:
    source = _load_json(SOURCE)
    symbols, configurations = _validate_source(source)
    labels, structure_hash = _load_structure_labels()
    source_hash = _sha256(SOURCE)
    protocol = frozen_protocol(
        source,
        source_sha256=source_hash,
        market_structure_protocol_hash=structure_hash,
    )
    digest = _protocol_hash(protocol)
    if OUT.exists():
        existing = _load_json(OUT)
        if existing.get("protocol_hash") != digest:
            raise ValueError("existing P14 artifact uses a different frozen protocol")

    dates = _available_fresh_dates(labels)
    gate = readiness_gate(len(dates))
    payload: dict[str, Any] = {
        "version": VERSION,
        "protocol_hash": digest,
        "protocol": protocol,
        "status": gate["status"],
        "readiness_gate": gate,
        "observation": None,
        "failures": [],
        "auto_apply": False,
    }
    if dates:
        results = []
        for key in FROZEN_KEYS:
            results.append(
                _run_candidate(
                    key,
                    str(configurations[key]["strategy_id"]),
                    symbols,
                    dates[0],
                    dates[-1],
                    labels,
                    len(dates),
                )
            )
        payload["observation"] = {
            "start": dates[0],
            "end": dates[-1],
            "fresh_trading_days": len(dates),
            "candidates": results,
        }
        payload["failures"] = [
            {"candidate": row["key"], "error": row["error"]}
            for row in results
            if row["status"] == "error"
        ]

    _atomic_write(payload)
    print(
        f"[structure-forward] {gate['status']} days={len(dates)} "
        f"remaining={gate['remaining_to_minimum']} -> {OUT}",
        flush=True,
    )
    return payload


if __name__ == "__main__":
    main()
