"""Run the frozen Selection MVP v2 cross-sectional walk-forward replay.

This is a research runner, not a production promotion command.  It compares a
fixed quality-momentum selector with the same selector plus a fixed 20% factor
overlay.  Every fold chooses factor on/off from its training interval only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.backtest.matrix import MatrixComputeCache, load_market_data_matrix_from_parquet
from app.strategy.builtin.custom_factor import MATRIX_STRATEGY as FACTOR_STRATEGY
from app.strategy.builtin.quality_momentum_v1 import compute_quality_components
from research.paths import DATA_DIR, SELECTION_ARTIFACTS_DIR, ensure_artifact_dirs
from research.selection.audit import CHECK_LABELS
from research.selection.mvp_v2 import (
    BASE_VARIANT,
    FACTOR_VARIANT,
    HORIZONS,
    PRIMARY_HORIZON,
    PRIMARY_TOP_K,
    TOP_KS,
    TradingCosts,
    aggregate_cohort_records,
    build_forward_open_labels,
    combine_factor_overlay,
    cross_sectional_percentiles,
    dynamic_universe_mask,
    evaluate_score_grid,
    generate_session_folds,
    instrument_windows,
    point_in_time_universe_mask,
    preferred_live_variant,
    select_variant_from_training,
    summarize_record_grid,
)

DEFAULT_START = date(2024, 9, 24)
DEFAULT_FACTOR_FORMULA = "MA60_DEV VOL_RATIO DECAY MA60_DEV ADD ADD SIGN MA60_DEV MOM5 SUB ADD"
FACTOR_WEIGHT = 0.20
TRAIN_SESSIONS = 120
TEST_SESSIONS = 40
STEP_SESSIONS = 40
IMPROVEMENT_MARGIN = 0.001
REPORT = SELECTION_ARTIFACTS_DIR / "selection_mvp_v2.json"
LATEST_AUDIT = SELECTION_ARTIFACTS_DIR / "selection_mvp_v2_latest_audit.csv"
DAILY_TOP = SELECTION_ARTIFACTS_DIR / "selection_mvp_v2_daily_top20.csv"


def _available_dates(root: Path) -> list[date]:
    output: list[date] = []
    for path in root.glob("date=*"):
        try:
            output.append(date.fromisoformat(path.name.removeprefix("date=")))
        except ValueError:
            continue
    if not output:
        raise ValueError(f"no date partitions found under {root}")
    return sorted(output)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    try:
        display = path.relative_to(DATA_DIR.parent)
    except ValueError:
        display = path
    return str(display).replace("\\", "/")


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _atomic_json(payload: Mapping[str, Any], path: Path = REPORT) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_csv(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _ranked_ids(score: np.ndarray, eligible: np.ndarray, symbols: Sequence[str]) -> np.ndarray:
    ids = np.flatnonzero(np.asarray(eligible, dtype=bool) & np.isfinite(score))
    if ids.size == 0:
        return ids
    symbol_values = np.asarray(symbols, dtype=str)
    return ids[np.lexsort((symbol_values[ids], -np.asarray(score)[ids]))]


def _historical_st_mask(
    *,
    market: Any,
    root: Path,
    required_start: date,
    required_end: date,
) -> tuple[np.ndarray, dict[str, Any]]:
    required_labels = {
        str(label)[:10]
        for label in market.timestamp_labels
        if str(required_start) <= str(label)[:10] <= str(required_end)
    }
    partition_paths = {
        path.parent.name.removeprefix("date="): path for path in root.glob("date=*/part.parquet")
    }
    missing = sorted(required_labels - set(partition_paths))
    if missing:
        preview = ", ".join(missing[:5])
        raise ValueError(f"historical stock_st coverage missing {len(missing)} sessions: {preview}")
    time_by_label = {
        str(label)[:10]: time_id for time_id, label in enumerate(market.timestamp_labels)
    }
    asset_by_symbol = {symbol: asset_id for asset_id, symbol in enumerate(market.symbols)}
    mask = np.zeros(market.shape, dtype=bool)
    row_count = 0
    matched_rows = 0
    digest = hashlib.sha256()
    for label in sorted(required_labels):
        path = partition_paths[label]
        file_hash = _sha256(path)
        digest.update(f"{label}:{file_hash}\n".encode())
        frame = pl.read_parquet(path, columns=["symbol", "trade_date"])
        row_count += frame.height
        time_id = time_by_label[label]
        for symbol in frame["symbol"].cast(pl.String).to_list():
            asset_id = asset_by_symbol.get(str(symbol).upper())
            if asset_id is not None:
                mask[time_id, asset_id] = True
                matched_rows += 1
    return mask, {
        "source": _relative(root),
        "required_range": [str(required_start), str(required_end)],
        "covered_sessions": len(required_labels),
        "rows": row_count,
        "matched_axis_rows": matched_rows,
        "dataset_manifest_sha256": digest.hexdigest(),
        "coverage_complete": True,
    }


def _record_rows(
    *,
    market: Any,
    record_map: Mapping[tuple[int, int], Sequence[Mapping[str, Any]]],
    scores: np.ndarray,
    eligible: np.ndarray,
    variant: str,
    costs: TradingCosts,
    labels: Mapping[int, Mapping[str, np.ndarray]],
) -> list[dict[str, Any]]:
    primary = {int(row["time_id"]): row for row in record_map[(PRIMARY_HORIZON, 20)]}
    rows: list[dict[str, Any]] = []
    for time_id in sorted(primary):
        ranked = _ranked_ids(scores[time_id], eligible[time_id], market.symbols)[:20]
        for rank, asset_id in enumerate(ranked, start=1):
            row: dict[str, Any] = {
                "signal_date": str(market.timestamp_labels[time_id])[:10],
                "variant": variant,
                "rank": rank,
                "symbol": market.symbols[asset_id],
                "name": market.names[asset_id],
                "score": round(float(scores[time_id, asset_id]), 8),
            }
            for horizon in HORIZONS:
                label = labels[horizon]
                if bool(label["valid"][time_id, asset_id]):
                    gross = float(label["gross_return"][time_id, asset_id])
                    row[f"return_{horizon}d_net"] = round(gross - costs.round_trip, 8)
                else:
                    row[f"return_{horizon}d_net"] = None
            rows.append(row)
    return rows


def _latest_audit_rows(
    *,
    market: Any,
    time_id: int,
    components: Mapping[str, Any],
    universe: np.ndarray,
    signal_eligible: np.ndarray,
    base_scores: np.ndarray,
    factor_scores: np.ndarray,
    combined_scores: np.ndarray,
    live_variant: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chosen_scores = combined_scores if live_variant == FACTOR_VARIANT else base_scores
    chosen_ids = _ranked_ids(chosen_scores[time_id], signal_eligible[time_id], market.symbols)
    rank_by_id = {int(asset_id): rank for rank, asset_id in enumerate(chosen_ids, start=1)}
    selected = set(int(value) for value in chosen_ids[:20])
    rows: list[dict[str, Any]] = []
    for asset_id, symbol in enumerate(market.symbols):
        if not bool(universe[time_id, asset_id]):
            reasons = ["不在当日动态股票池(停牌/未上市/已退市/当前ST代理过滤)"]
            decision = "rejected_universe"
        else:
            failed = [
                CHECK_LABELS.get(check, check)
                for check, mask in components["checks"].items()
                if not bool(mask[time_id, asset_id])
            ]
            if failed:
                reasons = failed
                decision = "rejected_signal"
            elif asset_id in selected:
                reasons = [f"{live_variant} 横截面排名进入 Top20"]
                decision = "selected_signal"
            else:
                reasons = ["信号有效, 但综合排名未进入 Top20"]
                decision = "rejected_rank"
        rows.append(
            {
                "signal_date": str(market.timestamp_labels[time_id])[:10],
                "symbol": symbol,
                "name": market.names[asset_id],
                "decision": decision,
                "rank": rank_by_id.get(asset_id),
                "quality_score": round(float(components["score"][time_id, asset_id]), 6),
                "base_percentile": _finite_round(base_scores[time_id, asset_id]),
                "factor_raw_score": _finite_round(factor_scores[time_id, asset_id]),
                "combined_score": _finite_round(combined_scores[time_id, asset_id]),
                "reasons": "; ".join(reasons),
            }
        )
    picked = sorted(
        (row for row in rows if row["decision"] == "selected_signal"),
        key=lambda row: int(row["rank"]),
    )
    return rows, picked


def _finite_round(value: Any, digits: int = 8) -> float | None:
    number = float(value)
    return round(number, digits) if math.isfinite(number) else None


def _regime_breakdown(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    source = DATA_DIR / ".regime_cache" / "market_structure_v1.parquet"
    if not source.exists():
        return {"available": False, "reason": "regime cache missing"}
    frame = pl.read_parquet(source, columns=["date", "source_date", "regime", "protocol_hash"])
    mapping = {str(day): str(regime) for day, regime in frame.select("date", "regime").iter_rows()}
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        grouped[mapping.get(str(row["signal_date"]), "unclassified")].append(row)
    return {
        "available": True,
        "source": _relative(source),
        "causal_lag": "cache date uses its disclosed source_date; regime is diagnostic only",
        "used_for_selection": False,
        "protocol_hashes": sorted(set(frame["protocol_hash"].drop_nulls().to_list())),
        "metrics": {
            regime: aggregate_cohort_records(group, horizon=PRIMARY_HORIZON)
            for regime, group in sorted(grouped.items())
        },
    }


def _protocol(start: date, end: date, costs: TradingCosts, *, universe_mode: str) -> dict[str, Any]:
    protocol: dict[str, Any] = {
        "version": 2,
        "status": "HISTORICAL_WALK_FORWARD_REPLAY_NOT_FRESH_OOS",
        "signal_timing": "score at close t; buy open t+1",
        "labels": {str(h): f"exit open t+{h + 1}" for h in HORIZONS},
        "range": [str(start), str(end)],
        "universe_mode": universe_mode,
        "top_k": list(TOP_KS),
        "folds": {
            "train_sessions": TRAIN_SESSIONS,
            "test_sessions": TEST_SESSIONS,
            "step_sessions": STEP_SESSIONS,
        },
        "variants": {
            BASE_VARIANT: "quality_momentum_v1 cross-sectional percentile",
            FACTOR_VARIANT: f"80% base percentile + {FACTOR_WEIGHT:.0%} fixed factor percentile",
        },
        "factor_formula": DEFAULT_FACTOR_FORMULA,
        "factor_gate": {
            "primary": {"horizon": PRIMARY_HORIZON, "top_k": PRIMARY_TOP_K},
            "objective": "train mean excess - 0.25 * abs(train worst phase max drawdown)",
            "improvement_margin": IMPROVEMENT_MARGIN,
            "test_metrics_used": False,
        },
        "trading_costs": costs.to_dict(),
        "seed": None,
        "determinism": "no random sampling; score ties break by symbol lexical order",
    }
    protocol["sha256"] = hashlib.sha256(
        json.dumps(protocol, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return protocol


def run(
    *,
    start: date = DEFAULT_START,
    end: date | None = None,
    universe_mode: str = "current_proxy",
    report_path: Path = REPORT,
    latest_audit_path: Path = LATEST_AUDIT,
    daily_top_path: Path = DAILY_TOP,
) -> dict[str, Any]:
    started = time.time()
    ensure_artifact_dirs()
    parquet_root = DATA_DIR / "kline_daily_enriched"
    stock_basic_source = DATA_DIR / "tushare_stock_basic" / "all.parquet"
    dates = _available_dates(parquet_root)
    requested_end = end or dates[-1]
    effective_end = min(requested_end, dates[-1])
    if start >= effective_end:
        raise ValueError("selection replay start must precede the latest local session")
    if not stock_basic_source.exists():
        raise ValueError(f"stock basic metadata missing: {stock_basic_source}")

    instruments = pl.read_parquet(stock_basic_source)
    windows = instrument_windows(instruments.to_dicts())
    symbols = sorted(windows)
    warmup_start = max(dates[0], start - timedelta(days=180))
    market = load_market_data_matrix_from_parquet(
        parquet_root,
        warmup_start,
        effective_end,
        field_columns={"amount", "turnover_rate"},
        symbols=symbols,
        instruments=instruments,
        cache_root=DATA_DIR / ".backtest_matrix_cache",
        coverage_start=warmup_start,
        coverage_end=effective_end,
        profile_generation="selection_mvp_v2",
    )
    present = (
        np.isfinite(market.close)
        & (market.close > 0)
        & np.isfinite(market.volume)
        & (market.volume > 0)
    )
    stock_st_status: dict[str, Any] | None = None
    if universe_mode == "current_proxy":
        dynamic_mask, universe_summary = dynamic_universe_mask(
            market.timestamp_labels, market.symbols, windows, present
        )
    elif universe_mode == "point_in_time":
        historical_st, stock_st_status = _historical_st_mask(
            market=market,
            root=DATA_DIR / "tushare_stock_st",
            required_start=start,
            required_end=effective_end,
        )
        dynamic_mask, universe_summary = point_in_time_universe_mask(
            market.timestamp_labels,
            market.symbols,
            windows,
            present,
            historical_st,
        )
    else:
        raise ValueError("universe_mode must be current_proxy or point_in_time")

    cache = MatrixComputeCache(max_bytes=768 * 1024 * 1024, max_item_bytes=256 * 1024 * 1024)
    try:
        with cache.activate(market):
            components = compute_quality_components(market, {})
            factor_signal = FACTOR_STRATEGY.compute_signals(
                market, {"factor_formula": DEFAULT_FACTOR_FORMULA}
            )
    finally:
        cache.close()
    base_eligible = np.asarray(components["eligible"], dtype=bool) & dynamic_mask
    base_scores = cross_sectional_percentiles(components["score"], base_eligible, market.symbols)
    factor_raw = np.asarray(factor_signal.score, dtype=np.float32)
    factor_percentile = cross_sectional_percentiles(factor_raw, base_eligible, market.symbols)
    combined_scores = combine_factor_overlay(
        base_scores, factor_percentile, factor_weight=FACTOR_WEIGHT
    )

    labels = build_forward_open_labels(market)
    session_ids = [
        index
        for index, label in enumerate(market.timestamp_labels)
        if str(label)[:10] >= str(start) and index < market.shape[0] - max(HORIZONS) - 1
    ]
    folds = generate_session_folds(
        session_ids,
        train_sessions=TRAIN_SESSIONS,
        test_sessions=TEST_SESSIONS,
        step_sessions=STEP_SESSIONS,
    )
    if not folds:
        raise ValueError("insufficient sessions for the frozen 120/40 walk-forward protocol")
    costs = TradingCosts()
    score_by_variant = {BASE_VARIANT: base_scores, FACTOR_VARIANT: combined_scores}
    full_metrics: dict[str, Any] = {}
    full_records: dict[str, Any] = {}
    for variant, score in score_by_variant.items():
        metrics, records = evaluate_score_grid(
            scores=score,
            eligible=base_eligible,
            symbols=market.symbols,
            timestamp_labels=market.timestamp_labels,
            forward_labels=labels,
            time_ids=session_ids,
            costs=costs,
        )
        full_metrics[variant] = metrics
        full_records[variant] = records

    fold_rows: list[dict[str, Any]] = []
    selected_test_records: list[dict[str, Any]] = []
    candidate_test_records: dict[str, list[dict[str, Any]]] = {BASE_VARIANT: [], FACTOR_VARIANT: []}
    votes: list[str] = []
    all_test_ids: list[int] = []
    for fold in folds:
        train_metrics: dict[str, Any] = {}
        test_metrics: dict[str, Any] = {}
        test_slices: dict[str, Any] = {}
        for variant in score_by_variant:
            train_metrics[variant], _ = summarize_record_grid(
                full_records[variant], time_ids=fold.train_ids
            )
            test_metrics[variant], test_slices[variant] = summarize_record_grid(
                full_records[variant], time_ids=fold.test_ids
            )
            candidate_test_records[variant].extend(
                test_slices[variant][(PRIMARY_HORIZON, PRIMARY_TOP_K)]
            )
        decision = select_variant_from_training(
            train_metrics, improvement_margin=IMPROVEMENT_MARGIN
        )
        selected_variant = decision["selected_variant"]
        votes.append(selected_variant)
        all_test_ids.extend(fold.test_ids)
        selected_test_records.extend(
            test_slices[selected_variant][(PRIMARY_HORIZON, PRIMARY_TOP_K)]
        )
        fold_rows.append(
            {
                "fold": fold.index,
                "train_range": [
                    str(market.timestamp_labels[fold.train_ids[0]])[:10],
                    str(market.timestamp_labels[fold.train_ids[-1]])[:10],
                ],
                "test_range": [
                    str(market.timestamp_labels[fold.test_ids[0]])[:10],
                    str(market.timestamp_labels[fold.test_ids[-1]])[:10],
                ],
                "train_decision": decision,
                "train_primary_metrics": {
                    variant: train_metrics[variant][str(PRIMARY_HORIZON)][str(PRIMARY_TOP_K)]
                    for variant in score_by_variant
                },
                "test_primary_metrics": {
                    variant: test_metrics[variant][str(PRIMARY_HORIZON)][str(PRIMARY_TOP_K)]
                    for variant in score_by_variant
                },
                "selected_test_metrics": test_metrics[selected_variant],
            }
        )

    live = preferred_live_variant(votes)
    live_variant = str(live["selected_variant"])
    latest_time_id = market.shape[0] - 1
    latest_rows, latest_picks = _latest_audit_rows(
        market=market,
        time_id=latest_time_id,
        components=components,
        universe=dynamic_mask,
        signal_eligible=base_eligible,
        base_scores=base_scores,
        factor_scores=factor_raw,
        combined_scores=combined_scores,
        live_variant=live_variant,
    )
    _atomic_csv(
        latest_audit_path,
        latest_rows,
        (
            "signal_date",
            "symbol",
            "name",
            "decision",
            "rank",
            "quality_score",
            "base_percentile",
            "factor_raw_score",
            "combined_score",
            "reasons",
        ),
    )
    daily_rows = _record_rows(
        market=market,
        record_map=full_records[live_variant],
        scores=score_by_variant[live_variant],
        eligible=base_eligible,
        variant=live_variant,
        costs=costs,
        labels=labels,
    )
    _atomic_csv(
        daily_top_path,
        daily_rows,
        (
            "signal_date",
            "variant",
            "rank",
            "symbol",
            "name",
            "score",
            "return_1d_net",
            "return_3d_net",
            "return_5d_net",
            "return_10d_net",
        ),
    )

    candidate_aggregate = {
        variant: aggregate_cohort_records(records, horizon=PRIMARY_HORIZON)
        for variant, records in candidate_test_records.items()
    }
    selected_aggregate = aggregate_cohort_records(selected_test_records, horizon=PRIMARY_HORIZON)
    effective_test_ids = sorted(set(all_test_ids))
    payload = {
        "status": "HISTORICAL_WALK_FORWARD_REPLAY_NOT_FRESH_OOS",
        "protocol": _protocol(start, effective_end, costs, universe_mode=universe_mode),
        "data": {
            "market_source": _relative(parquet_root),
            "stock_basic_source": _relative(stock_basic_source),
            "loaded_range": [
                str(market.timestamp_labels[0])[:10],
                str(market.timestamp_labels[-1])[:10],
            ],
            "evaluation_range": [
                str(market.timestamp_labels[session_ids[0]])[:10],
                str(market.timestamp_labels[session_ids[-1]])[:10],
            ],
            "axis_symbols": len(market.symbols),
            "daily_dynamic_universe": {
                "minimum": int(dynamic_mask.sum(axis=1).min()),
                "median": int(np.median(dynamic_mask.sum(axis=1))),
                "maximum": int(dynamic_mask.sum(axis=1).max()),
            },
            "universe_filter": universe_summary,
            "universe_mode": universe_mode,
            "stock_st": stock_st_status,
            "point_in_time_gap": (
                None
                if universe_mode == "point_in_time"
                else (
                    "local data has listing/delisting dates but no historical ST/name-change "
                    "intervals; current non-ST status is used as a disclosed proxy"
                )
            ),
        },
        "score_definition": {
            "base": "existing quality_momentum_v1 executable score, ranked cross-sectionally",
            "optional_factor": {
                "formula": DEFAULT_FACTOR_FORMULA,
                "weight": FACTOR_WEIGHT,
                "not_a_standalone_strategy": True,
            },
            "future_labels_used_in_ranking": False,
        },
        "folds": fold_rows,
        "walk_forward_test": {
            "session_range": [
                str(market.timestamp_labels[effective_test_ids[0]])[:10],
                str(market.timestamp_labels[effective_test_ids[-1]])[:10],
            ],
            "candidate_primary_metrics": candidate_aggregate,
            "train_selected_primary_metrics": selected_aggregate,
            "selection_counts": live["train_fold_votes"],
            "same_candidate_evaluation_budget": True,
        },
        "regime_diagnostic": _regime_breakdown(selected_test_records),
        "next_observation_selector": live,
        "latest_signal": {
            "date": str(market.timestamp_labels[latest_time_id])[:10],
            "variant": live_variant,
            "execution_status": "close-data signal only; no order placed",
            "auction_data_used": False,
            "top20": latest_picks,
        },
        "artifacts": {
            "latest_audit_csv": _relative(latest_audit_path),
            "daily_top20_csv": _relative(daily_top_path),
        },
        "leakage_barriers": [
            "signals and ranks are built before forward labels are read",
            "invalid future fills remain missing and never trigger replacement selection",
            "factor on/off decisions receive train metrics only",
            "test metrics do not tune thresholds, formula, weight, K, horizon, or early stopping",
            "regime labels are diagnostic and never alter scores or fold decisions",
        ],
        "failures": [],
        "production_default_changed": False,
        "fresh_oos_status": "NOT_STARTED",
    }
    payload["artifacts"]["latest_audit_sha256"] = _sha256(latest_audit_path)
    payload["artifacts"]["daily_top20_sha256"] = _sha256(daily_top_path)
    _atomic_json(payload, report_path)
    print(
        f"[selection-mvp-v2] {payload['status']} in {time.time() - started:.1f}s -> {report_path}",
        flush=True,
    )
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = _parse_args()
    run(start=arguments.start, end=arguments.end)
