"""结构牛/结构熊双腿策略的 canonical historical replay。

结构标签协议先于本脚本固定，不读取策略收益调阈值。F1-F4 已被历史研究使用，
因此结果只用于比较和否证，不构成 fresh OOS 晋级。
"""
from __future__ import annotations

import json
import math
import time
from typing import Any

import numpy as np
import polars as pl

from app.backtest.strategy import StrategyBacktestConfig
from app.backtest.worker import make_worker_task, run_worker_task
from app.config import settings
from research.factors.run_factor_engine_wf import fold_dates, select_universe
from research.paths import DATA_DIR, REGIME_ARTIFACTS_DIR
from research.regime.run_regime_ensemble import derive_metrics

OUTPUT = REGIME_ARTIFACTS_DIR / "structure_strategy_replay_v1.json"
CACHE = DATA_DIR / ".regime_cache" / "market_structure_v1.parquet"
BASE_OVERRIDES = {
    "stop_loss": -0.05,
    "take_profit": None,
    "trailing_stop": None,
    "max_hold_days": 20,
}


def _protocol_hash() -> str:
    cache = pl.read_parquet(CACHE, columns=["protocol_hash"])
    values = cache["protocol_hash"].drop_nulls().cast(pl.Utf8).unique().to_list()
    if len(values) != 1:
        raise ValueError("market structure cache must contain one protocol hash")
    return values[0]


def _cash_filter(protocol_hash: str) -> dict[str, Any]:
    return {
        "type": "market_structure_v1",
        "protocol_hash": protocol_hash,
        "mode": "soft",
        "bear_weight": 0.0,
        "scale_existing": True,
    }


def _switch_composition(
    bull_strategy: str,
    bear_strategy: str,
    protocol_hash: str,
) -> dict[str, Any]:
    return {
        "entry_mode": "regime_switch",
        "score_mode": "active_score",
        "regime": {
            "type": "market_structure_v1",
            "protocol_hash": protocol_hash,
        },
        "components": [
            {"strategy_id": bull_strategy},
            {"strategy_id": bear_strategy},
        ],
    }


def configurations(protocol_hash: str) -> tuple[dict[str, Any], ...]:
    configs: list[dict[str, Any]] = []
    for bull_strategy, prefix in (
        ("bullish_alignment", "bullish"),
        ("trend_breakout", "trend"),
    ):
        configs.extend(
            [
                {
                    "key": f"{prefix}_always",
                    "strategy_id": bull_strategy,
                    "regime_filter": None,
                    "composition": None,
                    "note": f"{bull_strategy} 全时段基线",
                },
                {
                    "key": f"{prefix}_bull_bear_cash",
                    "strategy_id": bull_strategy,
                    "regime_filter": _cash_filter(protocol_hash),
                    "composition": None,
                    "note": f"结构牛={bull_strategy}；结构熊=现金",
                },
                {
                    "key": f"{prefix}_bull_bear_pullback",
                    "strategy_id": bull_strategy,
                    "regime_filter": None,
                    "composition": _switch_composition(
                        bull_strategy,
                        "pullback_to_support",
                        protocol_hash,
                    ),
                    "note": f"结构牛={bull_strategy}；结构熊=pullback_to_support",
                },
            ]
        )
    configs.append(
        {
            "key": "pullback_always",
            "strategy_id": "pullback_to_support",
            "regime_filter": None,
            "composition": None,
            "note": "pullback_to_support 全时段基线",
        }
    )
    return tuple(configs)


def _run_one(config: dict[str, Any], start, end, symbols) -> tuple[dict | None, str | None]:
    backtest = StrategyBacktestConfig(
        strategy_id=config["strategy_id"],
        symbols=symbols,
        start=start,
        end=end,
        params=None,
        overrides=dict(BASE_OVERRIDES),
        composition=config["composition"],
        matching="open_t+1",
        entry_fill=None,
        exit_fill=None,
        fees_pct=0.0002,
        commission_pct=None,
        stamp_tax_pct=None,
        slippage_bps=5.0,
        max_positions=10,
        max_exposure_pct=1.0,
        initial_capital=1_000_000.0,
        position_sizing="score_weight",
        mode="position",
        holding_days=20,
        asset_type="stock",
        minute_fill=False,
        regime_filter=config["regime_filter"],
    )
    result = run_worker_task(make_worker_task("backtest", settings.data_dir, backtest))
    if result.get("error"):
        return None, str(result["error"])
    return derive_metrics(result), None


def _aggregate(key: str, folds: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [
        run
        for fold in folds
        for run in fold["runs"]
        if run["key"] == key and "error" not in run
    ]
    returns = [float(row["total_return"]) for row in rows]
    return {
        "valid_folds": len(rows),
        "positive_folds": sum(value > 0 for value in returns),
        "mean_total_return": round(float(np.mean(returns)), 4) if returns else None,
        "compounded_return": (
            round(float(np.prod([1.0 + value for value in returns]) - 1.0), 4)
            if returns
            else None
        ),
        "mean_sharpe": (
            round(float(np.mean([row["sharpe"] for row in rows])), 3) if rows else None
        ),
        "mean_max_drawdown": (
            round(float(np.mean([row["max_drawdown"] for row in rows])), 4)
            if rows
            else None
        ),
        "total_trades": sum(int(row["n_trades"]) for row in rows),
    }


def _comparison(
    candidate: str,
    baseline: str,
    folds: list[dict[str, Any]],
    aggregate: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    deltas: list[float] = []
    beats = 0
    for fold in folds:
        candidate_row = next(
            (
                row
                for row in fold["runs"]
                if row["key"] == candidate and "error" not in row
            ),
            None,
        )
        baseline_row = next(
            (
                row
                for row in fold["runs"]
                if row["key"] == baseline and "error" not in row
            ),
            None,
        )
        if candidate_row is None or baseline_row is None:
            continue
        delta = float(candidate_row["total_return"]) - float(baseline_row["total_return"])
        deltas.append(delta)
        beats += int(delta > 0)
    valid = len(deltas)
    passed = bool(
        valid == len(folds)
        and beats >= 3
        and aggregate[candidate]["positive_folds"] >= 3
        and float(np.mean(deltas)) > 0
        and aggregate[candidate]["compounded_return"] > aggregate[baseline]["compounded_return"]
    )
    return {
        "candidate": candidate,
        "baseline": baseline,
        "valid_folds": valid,
        "beats_baseline_folds": beats,
        "mean_return_delta": round(float(np.mean(deltas)), 4) if deltas else None,
        "historical_gate_pass": passed,
        "auto_apply": False,
    }


def _strict(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _strict(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strict(item) for item in value]
    return value


def main() -> None:
    started = time.time()
    protocol_hash = _protocol_hash()
    configs = configurations(protocol_hash)
    symbols = select_universe()
    folds = fold_dates()
    fold_rows: list[dict[str, Any]] = []
    for fold_id, train_start, train_end, test_start, test_end in folds:
        print(f"[structure-replay] {fold_id} {test_start}~{test_end}", flush=True)
        record = {
            "fold": fold_id,
            "train": [train_start, train_end],
            "test": [test_start, test_end],
            "runs": [],
        }
        for config in configs:
            metrics, error = _run_one(config, test_start, test_end, symbols)
            if error:
                print(f"  FAIL {config['key']}: {error}", flush=True)
                record["runs"].append({"key": config["key"], "error": error})
            else:
                print(
                    f"  {config['key']:<28} ret={metrics['total_return']:+.2%} "
                    f"MDD={metrics['max_drawdown']:+.2%} n={metrics['n_trades']}",
                    flush=True,
                )
                record["runs"].append({"key": config["key"], **metrics})
        fold_rows.append(record)

    aggregate = {config["key"]: _aggregate(config["key"], fold_rows) for config in configs}
    comparisons = [
        _comparison("bullish_bull_bear_cash", "bullish_always", fold_rows, aggregate),
        _comparison("bullish_bull_bear_pullback", "bullish_always", fold_rows, aggregate),
        _comparison("trend_bull_bear_cash", "trend_always", fold_rows, aggregate),
        _comparison("trend_bull_bear_pullback", "trend_always", fold_rows, aggregate),
        _comparison(
            "bullish_bull_bear_pullback",
            "bullish_bull_bear_cash",
            fold_rows,
            aggregate,
        ),
        _comparison(
            "trend_bull_bear_pullback",
            "trend_bull_bear_cash",
            fold_rows,
            aggregate,
        ),
    ]
    payload = {
        "version": "structure_strategy_replay_v1",
        "status": "complete",
        "evidence_status": "canonical_historical_replay_not_fresh_oos",
        "market_structure_protocol_hash": protocol_hash,
        "universe": {
            "size": len(symbols),
            "seed": 20260723,
            "symbols": symbols,
        },
        "configurations": configs,
        "folds": fold_rows,
        "aggregate": aggregate,
        "comparisons": comparisons,
        "promotion_summary": (
            "HISTORICAL_CANDIDATE_ONLY"
            if any(item["historical_gate_pass"] for item in comparisons[:4])
            else "REJECTED_HISTORICAL_REPLAY"
        ),
        "auto_apply": False,
        "elapsed_seconds": round(time.time() - started, 2),
        "note": (
            "结构标签只用前一交易日数据；阈值未按本脚本收益调整。"
            "F1-F4 已被历史研究使用，任何通过项也只能成为未来观察候选。"
        ),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(_strict(payload), ensure_ascii=False, indent=2, default=str, allow_nan=False),
        encoding="utf-8",
    )
    print(f"[structure-replay] done | {OUTPUT}", flush=True)


if __name__ == "__main__":
    main()
