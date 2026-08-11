"""Run the fixed, explainable stock-selection v1 historical comparison and latest audit.

There is deliberately no parameter search in this script.  All replay windows have
already been observed, so their results can diagnose the selector but cannot promote it.
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
from datetime import date
from typing import Any

import polars as pl

from app.backtest.strategy import StrategyBacktestConfig
from app.strategy.builtin.quality_momentum_v1 import compute_quality_components
from research.common.universe import symbols_sha256
from research.optimization.run_core_portfolio_walkforward_v1 import (
    TRIAL_POLICY,
    _make_service,
    _result_stats,
)
from research.optimization.run_core_strategy_walkforward_v1 import BACKTEST_KWARGS
from research.paths import DATA_DIR, SELECTION_ARTIFACTS_DIR, ensure_artifact_dirs
from research.selection.audit import build_decision_rows
from scripts.run_mvp import select_universe


OUT = SELECTION_ARTIFACTS_DIR / "selection_logic_v1.json"
AUDIT_CSV = SELECTION_ARTIFACTS_DIR / "selection_logic_v1_latest_audit.csv"
BASELINE_ID = "bullish_alignment"
CANDIDATE_ID = "quality_momentum_v1"
MAX_POSITIONS = 10
MAX_PER_CURRENT_INDUSTRY = 2

# These ranges were all visible before this experiment. They are diagnostics only.
WINDOWS = (
    ("leader_bull_2025_a", date(2025, 5, 24), date(2025, 8, 24)),
    ("leader_bull_2025_b", date(2025, 7, 24), date(2025, 10, 24)),
    ("target_2026_contaminated", date(2026, 3, 24), date(2026, 6, 24)),
    ("post_target_audit", date(2026, 6, 25), date(2026, 8, 7)),
)


def _config(strategy_id: str, symbols: list[str], start: date, end: date) -> StrategyBacktestConfig:
    kwargs = dict(BACKTEST_KWARGS)
    kwargs["max_positions"] = MAX_POSITIONS
    kwargs["position_sizing"] = "equal"
    return StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=symbols,
        start=start,
        end=end,
        params=None,
        overrides=None,
        **kwargs,
    )


def _stats_row(service, config, prepared) -> dict[str, Any]:
    stats, error, status = _result_stats(
        service.run(config, prepared=prepared, result_policy=TRIAL_POLICY)
    )
    row: dict[str, Any] = {"status": status, "stats": stats}
    if error is not None:
        row["error"] = error
    return row


def _industry_map() -> tuple[dict[str, str], dict[str, Any]]:
    source = DATA_DIR / "tushare_stock_basic" / "all.parquet"
    if not source.exists():
        return {}, {"available": False, "reason": "tushare_stock_basic/all.parquet missing"}
    frame = pl.read_parquet(source, columns=["ts_code", "industry"])
    mapping = {
        str(code).upper(): str(industry)
        for code, industry in frame.iter_rows()
        if code and industry
    }
    return mapping, {
        "available": True,
        "source": str(source.relative_to(DATA_DIR.parent)).replace("\\", "/"),
        "point_in_time": False,
        "usage": "latest-date display/diversification only; excluded from historical score and replay",
        "mapped_symbols": len(mapping),
    }


def _news_status() -> dict[str, Any]:
    source = DATA_DIR / "news" / "events.jsonl"
    return {
        "available": source.exists(),
        "enabled_in_score": False,
        "expected_source": str(source.relative_to(DATA_DIR.parent)).replace("\\", "/"),
        "required_fields": ["published_at", "ts_code", "source", "event_type", "score"],
        "reason": (
            "historical point-in-time news exists but remains disabled until a separate frozen replay"
            if source.exists()
            else "no historical point-in-time news archive; neutral score, no fabricated signal"
        ),
    }


def _protocol() -> dict[str, Any]:
    protocol = {
        "version": 1,
        "evidence_status": "historical_replay_only_all_windows_already_observed",
        "selection_change": "fixed quality/risk scorecard; no parameter search",
        "baseline": BASELINE_ID,
        "candidate": CANDIDATE_ID,
        "same_budget": "one baseline and one candidate evaluation per window",
        "universe": "all end-date listed non-ST stocks; no sampling",
        "seed": None,
        "deterministic_tie_break": "score descending, symbol lexical",
        "portfolio": {
            "historical_replay": {"max_positions": MAX_POSITIONS, "position_sizing": "equal"},
            "latest_display_only": {"max_per_current_industry": MAX_PER_CURRENT_INDUSTRY},
        },
        "windows": [
            {"id": window_id, "range": [str(start), str(end)]}
            for window_id, start, end in WINDOWS
        ],
        "leakage_barriers": [
            "no replay result selects or changes a threshold",
            "current static industry classification never enters historical score",
            "news requires timezone-aware published_at and is disabled without historical archive",
            "daily signal uses only current/prior bars; engine executes on the next bar",
            "no automatic production promotion",
        ],
    }
    protocol["sha256"] = hashlib.sha256(
        json.dumps(protocol, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return protocol


def _write_csv(rows: list[dict[str, Any]]) -> None:
    fields = [
        "signal_date", "symbol", "name", "industry_current", "decision",
        "eligible_rank", "score", "reasons", "momentum_20d", "momentum_60d",
        "ma20_bias", "ma20_slope_10d", "annual_vol_20d", "volume_ratio_5d",
        "drawdown_20d", "gap", "amount_20d", "trend_quality", "momentum_quality",
        "volume_confirmation", "liquidity_quality", "overextension_penalty",
        "volatility_penalty", "drawdown_penalty", "gap_penalty",
        "extreme_momentum_penalty",
    ]
    temporary = AUDIT_CSV.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(rows, key=lambda item: item["symbol"]):
            writer.writerow({**row, "reasons": "；".join(row["reasons"])})
    temporary.replace(AUDIT_CSV)


def _atomic_write(payload: dict[str, Any]) -> None:
    temporary = OUT.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(OUT)


def main() -> dict[str, Any]:
    started = time.time()
    ensure_artifact_dirs()
    protocol = _protocol()
    failures: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    store, service = _make_service()
    try:
        for window_id, start, end in WINDOWS:
            symbols = select_universe(DATA_DIR, start=start, end=end, size=None, seed=None)
            configs = [
                _config(BASELINE_ID, symbols, start, end),
                _config(CANDIDATE_ID, symbols, start, end),
            ]
            baseline_prepared = service.prepare_matrix_optimization([configs[0]])
            candidate_prepared = service.prepare_matrix_optimization([configs[1]])
            try:
                baseline = _stats_row(service, configs[0], baseline_prepared)
                candidate = _stats_row(service, configs[1], candidate_prepared)
            finally:
                baseline_prepared.compute_cache.close()
                candidate_prepared.compute_cache.close()
            row = {
                "id": window_id,
                "range": [str(start), str(end)],
                "universe": {
                    "selection": "all end-date listed non-ST stocks; no sampling",
                    "size": len(symbols),
                    "sha256": symbols_sha256(symbols),
                },
                "evaluation_budget": {BASELINE_ID: 1, CANDIDATE_ID: 1},
                "baseline": baseline,
                "candidate": candidate,
            }
            if baseline.get("stats") and candidate.get("stats"):
                row["delta"] = {
                    key: round(float(candidate["stats"][key]) - float(baseline["stats"][key]), 8)
                    for key in ("total_return", "win_rate", "max_drawdown", "sharpe")
                }
            comparisons.append(row)
            for role, result in (("baseline", baseline), ("candidate", candidate)):
                if result["status"] == "error":
                    failures.append({"window_id": window_id, "role": role, "error": result["error"]})

        latest = max(end for _, _, end in WINDOWS)
        latest_symbols = select_universe(DATA_DIR, start=latest, end=latest, size=None, seed=None)
        latest_config = _config(CANDIDATE_ID, latest_symbols, latest, latest)
        prepared = service.prepare_matrix_optimization([latest_config])
        try:
            market = prepared.market_data
            time_id = max(
                index for index, label in enumerate(market.timestamp_labels) if label <= str(latest)
            )
            with prepared.compute_cache.activate(market):
                components = compute_quality_components(market, {})
            industries, industry_status = _industry_map()
            audit_rows, audit_summary = build_decision_rows(
                market=market,
                result=components,
                time_id=time_id,
                industries=industries,
                max_positions=MAX_POSITIONS,
                max_per_industry=MAX_PER_CURRENT_INDUSTRY,
            )
        finally:
            prepared.compute_cache.close()
    finally:
        store.db.close()

    _write_csv(audit_rows)
    selected_rows = sorted(
        (row for row in audit_rows if row["decision"] == "selected_signal"),
        key=lambda row: (row["eligible_rank"], row["symbol"]),
    )
    payload = {
        "status": "HISTORICAL_REPLAY_ONLY",
        "protocol": protocol,
        "score_definition": {
            "positive": "32% trend + 28% momentum quality + 12% volume confirmation + 8% liquidity",
            "penalties": "8% overextension + 5% volatility + 3% drawdown + 2% gap + 2% extreme momentum",
            "range": [0, 100],
            "thresholds": components["thresholds"],
            "weights_selected_without_replay_search": True,
        },
        "historical_comparisons": comparisons,
        "latest_audit": {
            "signal_date": str(latest),
            "execution_status": "signal_only; no next trading bar is available for a fill",
            "universe_sha256": symbols_sha256(latest_symbols),
            "summary": audit_summary,
            "selected": selected_rows,
            "csv": str(AUDIT_CSV.relative_to(DATA_DIR.parent)).replace("\\", "/"),
            "industry": industry_status,
            "news": _news_status(),
        },
        "failures": failures,
        "production_default_changed": False,
        "fresh_oos_status": "NOT_STARTED",
        "elapsed_seconds": round(time.time() - started, 1),
        "conclusion": (
            "Selection logic is executable and auditable. Historical replay is diagnostic only; "
            "a future frozen observation period is required before any promotion."
        ),
    }
    _atomic_write(payload)
    print(f"[selection-v1] {payload['status']} -> {OUT}", flush=True)
    return payload


if __name__ == "__main__":
    main()
