"""Fixed-protocol validation for matrix strategy composition v1.

This experiment deliberately does not tune weights.  It replays the historical
four-fold comparison on the established 400-symbol universe, then reports the
post-2026-06-30 data as a separate short unseen observation.  The historical
folds have already influenced the chosen mom_trend factor and therefore are
not described as fresh OOS evidence.

Run from ``backend/``::

    TICKFLOW_BACKTEST_MODE=inprocess .venv/Scripts/python.exe \
        -m research.validation.run_strategy_composition_wf --resume
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import date
from typing import Any

import numpy as np
import polars as pl

from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task
from app.config import settings
from research.common.universe import stable_symbol_sample, universe_manifest
from research.paths import DATA_DIR, VALIDATION_ARTIFACTS_DIR, ensure_artifact_dirs

OUT = VALIDATION_ARTIFACTS_DIR / "strategy_composition_wf_v1.json"
N_SYM = 400
SEED = 20260723
FULL0 = date(2024, 9, 24)
HISTORICAL_END = date(2026, 6, 30)
N_FOLDS = 4
TRAIN_SKIP_TD = 80
MIN_FRESH_OOS_TRADING_DAYS = 60
TARGET_FRESH_OOS_TRADING_DAYS = 120
MOM_TREND = "MOM20 MA60_DEV SIGN MUL"
BASE_WEIGHT = 0.3
FACTOR_WEIGHT = 0.7
BASE_OVERRIDES = {
    "stop_loss": -0.05,
    "take_profit": None,
    "trailing_stop": None,
    "max_hold_days": 20,
}
LEADER_FLAT = {
    "type": "leader_index",
    "ma": 60,
    "mode": "soft",
    "bear_weight": 0.0,
    "scale_existing": True,
}


def _composition(primary: str) -> dict[str, Any]:
    return {
        "entry_mode": "and",
        "score_mode": "weighted_rank",
        "components": [
            {"strategy_id": primary, "weight": BASE_WEIGHT},
            {
                "strategy_id": "custom_factor",
                "weight": FACTOR_WEIGHT,
                "params": {"factor_formula": MOM_TREND},
            },
        ],
    }


CONFIGS: tuple[dict[str, Any], ...] = (
    {
        "key": "bullish_alignment",
        "strategy_id": "bullish_alignment",
        "max_positions": 10,
        "note": "多头排列基线",
    },
    {
        "key": "bullish_factor_30_70",
        "strategy_id": "bullish_alignment",
        "max_positions": 10,
        "composition": _composition("bullish_alignment"),
        "note": "多头排列门控 + mom_trend 排序（固定 30/70）",
    },
    {
        "key": "pullback_to_support",
        "strategy_id": "pullback_to_support",
        "max_positions": 5,
        "note": "回踩支撑基线",
    },
    {
        "key": "pullback_factor_30_70",
        "strategy_id": "pullback_to_support",
        "max_positions": 5,
        "composition": _composition("pullback_to_support"),
        "note": "回踩支撑门控 + mom_trend 排序（固定 30/70）",
    },
    {
        "key": "flat_leader",
        "strategy_id": "custom_factor",
        "max_positions": 10,
        "params": {"factor_formula": MOM_TREND},
        "regime_filter": LEADER_FLAT,
        "note": "flat_leader 历史对照基线（不代表已晋级）",
    },
    {
        "key": "bullish_factor_flat_leader",
        "strategy_id": "bullish_alignment",
        "max_positions": 10,
        "composition": _composition("bullish_alignment"),
        "regime_filter": LEADER_FLAT,
        "note": "多头排列 + mom_trend 排序 + leader 熊市空仓",
    },
)

CONTRASTS = {
    "bullish_factor_minus_base": ("bullish_factor_30_70", "bullish_alignment"),
    "pullback_factor_minus_base": ("pullback_factor_30_70", "pullback_to_support"),
    "bullish_factor_flat_minus_flat_leader": (
        "bullish_factor_flat_leader",
        "flat_leader",
    ),
}


def _scan() -> pl.LazyFrame:
    return pl.scan_parquet(
        str(DATA_DIR / "kline_daily_enriched" / "**" / "*.parquet"),
        hive_partitioning=True,
    )


def select_universe() -> list[str]:
    symbols = (
        _scan()
        .filter((pl.col("date") >= FULL0) & (pl.col("date") <= HISTORICAL_END))
        .select("symbol")
        .unique()
        .sort("symbol")
        .collect()["symbol"]
        .to_list()
    )
    return stable_symbol_sample(symbols, N_SYM, SEED)


def fold_dates() -> list[dict[str, Any]]:
    dates = sorted(
        value
        for value in _scan().select("date").unique().collect()["date"].to_list()
        if FULL0 <= value <= HISTORICAL_END
    )
    remaining = dates[TRAIN_SKIP_TD:]
    chunk = len(remaining) // N_FOLDS
    folds: list[dict[str, Any]] = []
    for index in range(N_FOLDS):
        start_id = index * chunk
        stop_id = (index + 1) * chunk if index < N_FOLDS - 1 else len(remaining)
        test = remaining[start_id:stop_id]
        train = dates[: TRAIN_SKIP_TD + start_id]
        folds.append({
            "phase": "historical_replay",
            "fold": f"F{index + 1}",
            "train_start": train[0],
            "train_end": train[-1],
            "test_start": test[0],
            "test_end": test[-1],
            "interpretation": "reused historical test fold; not fresh OOS",
        })
    return folds


def unseen_observation() -> dict[str, Any] | None:
    dates = sorted(
        value
        for value in _scan().select("date").unique().collect()["date"].to_list()
        if value > HISTORICAL_END
    )
    if not dates:
        return None
    return {
        "phase": "unseen_observation",
        "fold": "OBS1",
        "train_start": FULL0,
        "train_end": HISTORICAL_END,
        "test_start": dates[0],
        "test_end": dates[-1],
        "trading_days": len(dates),
        "interpretation": "fresh but short observation; not a promotion gate",
    }


def promotion_gate(periods: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe readiness only; never infer promotion from an undersized window."""
    observed_days = max(
        (
            int(period.get("trading_days", 0))
            for period in periods
            if period.get("phase") == "unseen_observation"
        ),
        default=0,
    )
    ready = observed_days >= MIN_FRESH_OOS_TRADING_DAYS
    return {
        "status": "READY_FOR_FROZEN_REVIEW" if ready else "PENDING_DATA",
        "observed_fresh_trading_days": observed_days,
        "minimum_fresh_trading_days": MIN_FRESH_OOS_TRADING_DAYS,
        "target_fresh_trading_days": TARGET_FRESH_OOS_TRADING_DAYS,
        "remaining_to_minimum": max(0, MIN_FRESH_OOS_TRADING_DAYS - observed_days),
        "auto_promote": False,
        "note": (
            "A ready status only permits a frozen review; it does not promote a strategy. "
            "Historical replay and undersized fresh windows cannot satisfy this gate."
        ),
    }


def derive_metrics(result: dict[str, Any]) -> dict[str, Any]:
    stats = result.get("stats") or {}
    return {
        "total_return": float(stats.get("total_return", 0.0)),
        "sharpe": float(stats.get("sharpe", 0.0)),
        "max_drawdown": float(stats.get("max_drawdown", 0.0)),
        "win_rate": float(stats.get("win_rate", 0.0)),
        "n_trades": int(stats.get("n_trades", 0)),
        "sortino": (
            float(stats["sortino"])
            if stats.get("sortino") is not None
            else None
        ),
        "calmar": (
            float(stats["calmar"])
            if stats.get("calmar") is not None
            else None
        ),
    }


def run_engine(
    config: dict[str, Any],
    *,
    start: date,
    end: date,
    symbols: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    backtest = StrategyBacktestConfig(
        strategy_id=config["strategy_id"],
        symbols=symbols,
        start=start,
        end=end,
        params=config.get("params"),
        overrides=dict(BASE_OVERRIDES),
        composition=config.get("composition"),
        matching="open_t+1",
        fees_pct=0.0002,
        slippage_bps=5.0,
        max_positions=int(config["max_positions"]),
        max_exposure_pct=1.0,
        initial_capital=1_000_000.0,
        position_sizing="score_weight",
        mode="position",
        holding_days=20,
        asset_type="stock",
        minute_fill=False,
        regime_filter=config.get("regime_filter"),
    )
    result = run_worker_task(make_worker_task("backtest", settings.data_dir, backtest))
    if result.get("error"):
        return None, str(result["error"])
    return derive_metrics(result), None


def aggregate(records: list[dict[str, Any]], phase: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for config in CONFIGS:
        rows = [
            row
            for row in records
            if row["phase"] == phase
            and row["config_key"] == config["key"]
            and "error" not in row
        ]
        output[config["key"]] = {
            "note": config["note"],
            "n_periods": len(rows),
            "positive_periods": sum(row["total_return"] > 0 for row in rows),
            "mean_total_return": (
                round(float(np.mean([row["total_return"] for row in rows])), 4)
                if rows else None
            ),
            "mean_sharpe": (
                round(float(np.mean([row["sharpe"] for row in rows])), 3)
                if rows else None
            ),
            "mean_max_drawdown": (
                round(float(np.mean([row["max_drawdown"] for row in rows])), 4)
                if rows else None
            ),
            "total_trades": sum(row["n_trades"] for row in rows),
        }
    return output


def contrasts(aggregate_rows: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for name, (candidate_key, baseline_key) in CONTRASTS.items():
        candidate = aggregate_rows[candidate_key]
        baseline = aggregate_rows[baseline_key]
        deltas = {}
        for field in ("mean_total_return", "mean_sharpe", "mean_max_drawdown"):
            left = candidate[field]
            right = baseline[field]
            deltas[field] = (
                round(float(left - right), 4)
                if left is not None and right is not None
                else None
            )
        output[name] = {
            "candidate": candidate_key,
            "baseline": baseline_key,
            "delta": deltas,
        }
    return output


def _canonical_protocol(symbols: list[str], periods: list[dict[str, Any]]) -> dict[str, Any]:
    manifest = universe_manifest(
        symbols,
        seed=SEED,
        requested_size=N_SYM,
        start=FULL0,
        end=HISTORICAL_END,
    )
    return {
        "version": 1,
        "seed": SEED,
        "universe_size": len(symbols),
        "universe_selection": manifest["selection"],
        "universe_sha256": manifest["symbols_sha256"],
        "universe_manifest": manifest,
        "historical_range": [str(FULL0), str(HISTORICAL_END)],
        "factor_formula": MOM_TREND,
        "weights": {"base": BASE_WEIGHT, "factor": FACTOR_WEIGHT},
        "entry_mode": "and",
        "exit_mode": "any",
        "overrides": BASE_OVERRIDES,
        "configs": list(CONFIGS),
        "periods": periods,
        "leakage_notice": (
            "mom_trend was selected using earlier research that included F1-F4; "
            "historical_replay is not fresh OOS. OBS1 is fresh but too short for promotion."
        ),
    }


def _protocol_hash(protocol: dict[str, Any]) -> str:
    payload = json.dumps(protocol, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_checkpoint(payload: dict[str, Any]) -> None:
    ensure_artifact_dirs()
    temporary = OUT.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(OUT)


def main(*, resume: bool = False) -> dict[str, Any]:
    started = time.time()
    symbols = select_universe()
    periods = fold_dates()
    observation = unseen_observation()
    if observation is not None:
        periods.append(observation)
    protocol = _canonical_protocol(symbols, periods)
    protocol_hash = _protocol_hash(protocol)
    records: list[dict[str, Any]] = []
    if resume and OUT.exists():
        previous = json.loads(OUT.read_text(encoding="utf-8"))
        if previous.get("protocol_hash") != protocol_hash:
            raise ValueError("existing checkpoint protocol does not match the frozen config")
        records = list(previous.get("records") or [])

    completed = {
        (row["phase"], row["fold"], row["config_key"])
        for row in records
    }
    print(
        f"[composition-wf] universe={len(symbols)} seed={SEED} "
        f"periods={len(periods)} configs={len(CONFIGS)} resume={resume}",
        flush=True,
    )
    for period in periods:
        print(
            f"\n[{period['phase']}] {period['fold']} "
            f"{period['test_start']}~{period['test_end']}",
            flush=True,
        )
        for config in CONFIGS:
            run_key = (period["phase"], period["fold"], config["key"])
            if run_key in completed:
                print(f"  [skip] {config['key']}", flush=True)
                continue
            metrics, error = run_engine(
                config,
                start=period["test_start"],
                end=period["test_end"],
                symbols=symbols,
            )
            row = {
                "phase": period["phase"],
                "fold": period["fold"],
                "test_start": period["test_start"],
                "test_end": period["test_end"],
                "config_key": config["key"],
            }
            if error is not None:
                row["error"] = error
                print(f"  [FAIL] {config['key']}: {error}", flush=True)
            else:
                row.update(metrics or {})
                print(
                    f"  {config['key']:<34} "
                    f"ret={row['total_return'] * 100:+.2f}% "
                    f"sharpe={row['sharpe']:+.2f} "
                    f"mdd={row['max_drawdown'] * 100:.2f}% "
                    f"trades={row['n_trades']}",
                    flush=True,
                )
            records.append(row)
            completed.add(run_key)
            _write_checkpoint({
                "protocol_hash": protocol_hash,
                "protocol": protocol,
                "records": records,
                "status": "running",
            })

    phases = sorted({period["phase"] for period in periods})
    aggregates = {phase: aggregate(records, phase) for phase in phases}
    payload = {
        "protocol_hash": protocol_hash,
        "protocol": protocol,
        "records": records,
        "aggregate": aggregates,
        "contrasts": {
            phase: contrasts(aggregates[phase])
            for phase in phases
        },
        "promotion_gate": promotion_gate(periods),
        "status": "complete",
        "elapsed_seconds": round(time.time() - started, 1),
    }
    _write_checkpoint(payload)
    print(f"\n[composition-wf] complete -> {OUT}", flush=True)
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    main(resume=args.resume)
