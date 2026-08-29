"""Causal N-shape pullback-depth research (Issue #49).

The detector is deliberately narrower than the earlier first-limit-up research:
it identifies a confirmed up-swing, a confirmed pullback, and only emits an event
when a later close breaks the prior swing high.  Unconfirmed tail pivots never
become events.  The module is research-only and never enters screening/trading.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from datetime import date, timedelta
from typing import Any, Literal

from app.services.n_shape_golden_phoenix import (
    PublishedDailyMarketResearchReader,
    _bars_to_dicts,
    _valid_manifest_sha256,
    _valid_source_provenance,
    _validate_keys_no_trading_tokens,
    resolve_n_shape_reader,
)

FACTOR_ID = "n_shape_pullback_depth_v1"
FACTOR_VERSION = 1
FACTOR_NAME = "N字回调深度分档（研究）"
REACHABILITY = "daily_price_only"
PROMOTED = False

DEPTH_A_BOUNDARY = 0.50
DEPTH_BC_BOUNDARY = 0.33
BUCKET_A = "A"
BUCKET_B = "B"
BUCKET_C = "C"
COHORT_UNSTRATIFIED = "unstratified"
COHORT_C_GOLDEN_PHOENIX = "bucket_c_golden_phoenix"
FORWARD_HORIZONS = (5, 10, 20)
DEFAULT_REVERSAL_MODE: Literal["fixed_pct", "atr_multiple"] = "fixed_pct"
DEFAULT_REVERSAL_VALUE = 0.08
ATR_PERIOD = 14
ZIGZAG_SENSITIVITY_VALUES = (0.05, 0.10)
BUCKET_SENSITIVITY_DELTA = 0.05
ROUND_TRIP_COST_BPS = 20.0
MIN_OOS_SAMPLES = 30
MIN_OOS_SYMBOLS = 10
CALENDAR_WARMUP_DAYS = 500
VOLUME_SHRINK_RATIO = 0.70
VOLUME_PRE20_RATIO = 0.90


def classify_depth_bucket(
    depth: float,
    *,
    a_boundary: float = DEPTH_A_BOUNDARY,
    bc_boundary: float = DEPTH_BC_BOUNDARY,
) -> str:
    """Return the preregistered A/B/C bucket with explicit edge semantics."""
    if not math.isfinite(depth) or depth < 0 or depth > 1:
        raise ValueError("depth must be finite and within [0, 1]")
    if not 0 <= bc_boundary < a_boundary <= 1:
        raise ValueError("depth boundaries must satisfy 0 <= bc < a <= 1")
    if depth > a_boundary:
        return BUCKET_A
    if depth >= bc_boundary:
        return BUCKET_B
    return BUCKET_C


def _true_range(rows: list[dict[str, Any]], index: int) -> float:
    row = rows[index]
    previous_close = rows[index - 1]["raw_close"] if index else row["raw_close"]
    return max(
        row["raw_high"] - row["raw_low"],
        abs(row["raw_high"] - previous_close),
        abs(row["raw_low"] - previous_close),
    )


def _atr(rows: list[dict[str, Any]], index: int) -> float | None:
    if index + 1 < ATR_PERIOD:
        return None
    start = index - ATR_PERIOD + 1
    return sum(_true_range(rows, i) for i in range(start, index + 1)) / ATR_PERIOD


def _reversal_fraction(
    rows: list[dict[str, Any]],
    index: int,
    reference_price: float,
    mode: str,
    value: float,
) -> float | None:
    if value <= 0 or reference_price <= 0:
        raise ValueError("reversal value and reference price must be positive")
    if mode == "fixed_pct":
        if value >= 1:
            raise ValueError("fixed_pct reversal must be below 1")
        return value
    if mode == "atr_multiple":
        atr = _atr(rows, index)
        return None if atr is None else value * atr / reference_price
    raise ValueError("reversal_mode must be fixed_pct or atr_multiple")


def _forward_outcomes(
    *,
    bars_by_date: dict[date, dict[str, Any]],
    calendar: list[date],
    confirm_date: date,
    swing_high: float,
    origin_low: float,
    cost_bps: float,
) -> dict[str, Any]:
    index_by_date = {day: index for index, day in enumerate(calendar)}
    confirm_index = index_by_date[confirm_date]
    entry_index = confirm_index + 1
    output: dict[str, Any] = {}
    for horizon in FORWARD_HORIZONS:
        prefix = f"forward_{horizon}d"
        output[f"{prefix}_return"] = None
        output[f"{prefix}_new_high"] = None
        output[f"{prefix}_structure_failure"] = None
        exit_index = entry_index + horizon - 1
        if entry_index >= len(calendar) or exit_index >= len(calendar):
            continue
        days = calendar[entry_index : exit_index + 1]
        if any(day not in bars_by_date for day in days):
            continue
        entry = bars_by_date[days[0]]["raw_open"]
        exit_close = bars_by_date[days[-1]]["raw_close"]
        output[f"{prefix}_return"] = exit_close / entry - 1 - cost_bps / 10_000.0
        output[f"{prefix}_new_high"] = any(
            bars_by_date[day]["raw_close"] > swing_high for day in days
        )
        output[f"{prefix}_structure_failure"] = any(
            bars_by_date[day]["raw_low"] < origin_low for day in days
        )
    return output


def _golden_phoenix_flag(
    rows: list[dict[str, Any]], origin_index: int, high_index: int, confirm_index: int
) -> tuple[bool, bool, dict[str, float | None]]:
    pullback_rows = rows[high_index + 1 : confirm_index]
    prior_rows = rows[max(0, origin_index - 20) : origin_index]
    if not pullback_rows or len(prior_rows) < 20:
        return False, False, {"pullback_vs_high": None, "pullback_vs_pre20": None}
    high_volume = rows[high_index]["volume"]
    pre20_average = sum(row["volume"] for row in prior_rows) / len(prior_rows)
    pullback_average = sum(row["volume"] for row in pullback_rows) / len(pullback_rows)
    if high_volume <= 0 or pre20_average <= 0:
        return False, False, {"pullback_vs_high": None, "pullback_vs_pre20": None}
    ratios = {
        "pullback_vs_high": pullback_average / high_volume,
        "pullback_vs_pre20": pullback_average / pre20_average,
    }
    return (
        ratios["pullback_vs_high"] <= VOLUME_SHRINK_RATIO
        and ratios["pullback_vs_pre20"] <= VOLUME_PRE20_RATIO,
        True,
        ratios,
    )


def detect_causal_swings(
    *,
    symbol: str,
    rows: list[dict[str, Any]],
    calendar: list[date],
    event_window: tuple[date, date],
    reversal_mode: str = DEFAULT_REVERSAL_MODE,
    reversal_value: float = DEFAULT_REVERSAL_VALUE,
    cost_bps: float = ROUND_TRIP_COST_BPS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Detect confirmed N events and structural failures using prefix-only state.

    A historical swing high is confirmed only after a causal reversal.  A
    pullback low becomes usable only when a later close breaks that swing high;
    therefore the final, unconfirmed zigzag leg is never emitted.
    """
    if not rows:
        return [], []
    rows = sorted(rows, key=lambda row: row["date"])
    calendar_index = {day: index for index, day in enumerate(calendar)}
    bars_by_date = {row["date"]: row for row in rows}
    events: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    phase = "seeking_up"
    origin_index = 0
    high_index = 0
    pullback_low_index: int | None = None

    for index, row in enumerate(rows):
        if row["date"] not in calendar_index:
            continue
        if phase == "seeking_up":
            if row["raw_low"] <= rows[origin_index]["raw_low"]:
                origin_index = index
            fraction = _reversal_fraction(
                rows, index, rows[origin_index]["raw_low"], reversal_mode, reversal_value
            )
            if fraction is not None and row["raw_close"] >= rows[origin_index]["raw_low"] * (
                1 + fraction
            ):
                high_index = max(range(origin_index, index + 1), key=lambda i: rows[i]["raw_high"])
                phase = "seeking_pullback"
            continue

        if phase == "seeking_pullback":
            if row["raw_high"] >= rows[high_index]["raw_high"]:
                high_index = index
            fraction = _reversal_fraction(
                rows, index, rows[high_index]["raw_high"], reversal_mode, reversal_value
            )
            if fraction is None or row["raw_close"] > rows[high_index]["raw_high"] * (1 - fraction):
                continue
            candidates = range(high_index + 1, index + 1)
            if not candidates:
                continue
            pullback_low_index = min(candidates, key=lambda i: rows[i]["raw_low"])
            if rows[pullback_low_index]["raw_low"] < rows[origin_index]["raw_low"]:
                if event_window[0] <= row["date"] <= event_window[1]:
                    failures.append(
                        {
                            "symbol": symbol,
                            "failure_date": row["date"],
                            "origin_date": rows[origin_index]["date"],
                            "swing_high_date": rows[high_index]["date"],
                            "reason": "pullback_broke_origin_low",
                        }
                    )
                origin_index = pullback_low_index
                phase = "seeking_up"
            else:
                phase = "seeking_breakout"
            continue

        assert pullback_low_index is not None
        if row["raw_low"] <= rows[pullback_low_index]["raw_low"]:
            pullback_low_index = index
        if rows[pullback_low_index]["raw_low"] < rows[origin_index]["raw_low"]:
            if event_window[0] <= row["date"] <= event_window[1]:
                failures.append(
                    {
                        "symbol": symbol,
                        "failure_date": row["date"],
                        "origin_date": rows[origin_index]["date"],
                        "swing_high_date": rows[high_index]["date"],
                        "reason": "pullback_broke_origin_low",
                    }
                )
            origin_index = pullback_low_index
            phase = "seeking_up"
            continue
        if row["raw_close"] <= rows[high_index]["raw_high"]:
            continue

        origin_low = rows[origin_index]["raw_low"]
        swing_high = rows[high_index]["raw_high"]
        pullback_low = rows[pullback_low_index]["raw_low"]
        denominator = swing_high - origin_low
        if denominator > 0 and event_window[0] <= row["date"] <= event_window[1]:
            depth = (swing_high - pullback_low) / denominator
            if 0 <= depth <= 1:
                golden, golden_available, volume_ratios = _golden_phoenix_flag(
                    rows, origin_index, high_index, index
                )
                event = {
                    "symbol": symbol,
                    "event_date": row["date"],
                    "origin_date": rows[origin_index]["date"],
                    "swing_high_date": rows[high_index]["date"],
                    "pullback_low_date": rows[pullback_low_index]["date"],
                    "depth": depth,
                    "bucket": classify_depth_bucket(depth),
                    "golden_phoenix": golden,
                    "golden_phoenix_available": golden_available,
                    "volume_ratios": volume_ratios,
                    "structure": {
                        "origin_low_raw": origin_low,
                        "swing_high_raw": swing_high,
                        "pullback_low_raw": pullback_low,
                    },
                    "forward": _forward_outcomes(
                        bars_by_date=bars_by_date,
                        calendar=calendar,
                        confirm_date=row["date"],
                        swing_high=swing_high,
                        origin_low=origin_low,
                        cost_bps=cost_bps,
                    ),
                }
                _validate_keys_no_trading_tokens(event)
                events.append(event)
        origin_index = pullback_low_index
        high_index = index
        phase = "seeking_pullback"

    return events, failures


def _split_dates(events: list[dict[str, Any]]) -> dict[date, str]:
    dates = sorted({event["event_date"] for event in events})
    if not dates:
        return {}
    validation_start = max(1, int(len(dates) * 0.60))
    test_start = max(validation_start + 1, int(len(dates) * 0.80))
    test_start = min(test_start, len(dates))
    return {
        day: "train" if index < validation_start else "validation" if index < test_start else "test"
        for index, day in enumerate(dates)
    }


def _cohort(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    if name == COHORT_UNSTRATIFIED:
        return events
    if name == COHORT_C_GOLDEN_PHOENIX:
        return [
            event for event in events if event["bucket"] == BUCKET_C and event["golden_phoenix"]
        ]
    return [event for event in events if event["bucket"] == name]


def _stats(events: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    key = f"forward_{horizon}d_return"
    values = [
        float(event["forward"][key]) for event in events if event["forward"].get(key) is not None
    ]
    symbols = {event["symbol"] for event in events if event["forward"].get(key) is not None}
    output: dict[str, Any] = {
        "count": len(values),
        "symbols": len(symbols),
        "mean": None,
        "median": None,
        "win_rate": None,
        "ci95_low": None,
        "ci95_high": None,
        "new_high_rate": None,
        "structure_failure_rate": None,
    }
    if not values:
        return output
    mean = statistics.fmean(values)
    std = statistics.stdev(values) if len(values) > 1 else 0.0
    half_width = 1.96 * std / math.sqrt(len(values))
    eligible = [event for event in events if event["forward"].get(key) is not None]
    output.update(
        {
            "mean": mean,
            "median": statistics.median(values),
            "win_rate": sum(value > 0 for value in values) / len(values),
            "ci95_low": mean - half_width,
            "ci95_high": mean + half_width,
            "new_high_rate": sum(
                bool(event["forward"][f"forward_{horizon}d_new_high"]) for event in eligible
            )
            / len(eligible),
            "structure_failure_rate": sum(
                bool(event["forward"][f"forward_{horizon}d_structure_failure"])
                for event in eligible
            )
            / len(eligible),
        }
    )
    return output


def _incremental(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, float | None]:
    if candidate["mean"] is None or baseline["mean"] is None:
        return {"mean": None, "ci95_low": None, "ci95_high": None}
    candidate_half = (candidate["ci95_high"] - candidate["ci95_low"]) / 2
    baseline_half = (baseline["ci95_high"] - baseline["ci95_low"]) / 2
    half = math.sqrt(candidate_half**2 + baseline_half**2)
    mean = candidate["mean"] - baseline["mean"]
    return {"mean": mean, "ci95_low": mean - half, "ci95_high": mean + half}


def _placebo(
    events: list[dict[str, Any]], universe: list[dict[str, Any]], horizon: int
) -> dict[str, Any]:
    key = f"forward_{horizon}d_return"
    observed = [event for event in events if event["forward"].get(key) is not None]
    pool = [event for event in universe if event["forward"].get(key) is not None]
    if not observed or len(pool) < len(observed):
        return {"iterations": 0, "observed_mean": None, "random_mean": None, "p_value": None}
    observed_mean = statistics.fmean(float(event["forward"][key]) for event in observed)
    ordered = sorted(
        pool,
        key=lambda event: hashlib.sha256(
            f"{event['symbol']}|{event['event_date']}".encode()
        ).hexdigest(),
    )
    means: list[float] = []
    iterations = min(100, len(ordered))
    for offset in range(iterations):
        sample = [ordered[(offset + index) % len(ordered)] for index in range(len(observed))]
        means.append(statistics.fmean(float(event["forward"][key]) for event in sample))
    return {
        "iterations": iterations,
        "observed_mean": observed_mean,
        "random_mean": statistics.fmean(means),
        "p_value": (sum(mean >= observed_mean for mean in means) + 1) / (len(means) + 1),
    }


def _research(events: list[dict[str, Any]], failures: list[dict[str, Any]]) -> dict[str, Any]:
    split_by_date = _split_dates(events)
    names = (BUCKET_A, BUCKET_B, BUCKET_C, COHORT_UNSTRATIFIED, COHORT_C_GOLDEN_PHOENIX)
    populations: dict[str, Any] = {}
    for name in names:
        members = _cohort(events, name)
        split_stats: dict[str, Any] = {}
        for split in ("train", "validation", "test"):
            subset = [event for event in members if split_by_date.get(event["event_date"]) == split]
            split_stats[split] = {
                str(horizon): _stats(subset, horizon) for horizon in FORWARD_HORIZONS
            }
        comparator_name = BUCKET_C if name == COHORT_C_GOLDEN_PHOENIX else COHORT_UNSTRATIFIED
        comparator = _cohort(events, comparator_name)
        increments: dict[str, Any] = {}
        for split in ("validation", "test"):
            subset = [
                event for event in comparator if split_by_date.get(event["event_date"]) == split
            ]
            increments[split] = _incremental(split_stats[split]["10"], _stats(subset, 10))
        test = split_stats["test"]["10"]
        if test["count"] < MIN_OOS_SAMPLES or test["symbols"] < MIN_OOS_SYMBOLS:
            verdict = "unavailable_insufficient_samples"
        elif name == COHORT_UNSTRATIFIED:
            verdict = (
                "accepted" if test["ci95_low"] is not None and test["ci95_low"] > 0 else "rejected"
            )
        else:
            validation_increment = increments["validation"]["mean"]
            test_lower = increments["test"]["ci95_low"]
            verdict = (
                "accepted"
                if validation_increment is not None
                and validation_increment > 0
                and test_lower is not None
                and test_lower > 0
                else "rejected"
            )
        populations[name] = {
            "count": len(members),
            "splits": split_stats,
            "incremental_10d_vs": comparator_name,
            "incremental_10d": increments,
            "test_placebo_10d": _placebo(
                [event for event in members if split_by_date.get(event["event_date"]) == "test"],
                [event for event in events if split_by_date.get(event["event_date"]) == "test"],
                10,
            ),
            "verdict": verdict,
        }
    bucket_sensitivity = []
    for shift in (-BUCKET_SENSITIVITY_DELTA, BUCKET_SENSITIVITY_DELTA):
        counts = {BUCKET_A: 0, BUCKET_B: 0, BUCKET_C: 0}
        for event in events:
            counts[
                classify_depth_bucket(
                    event["depth"],
                    a_boundary=DEPTH_A_BOUNDARY + shift,
                    bc_boundary=DEPTH_BC_BOUNDARY + shift,
                )
            ] += 1
        bucket_sensitivity.append(
            {
                "shift": shift,
                "a_boundary": DEPTH_A_BOUNDARY + shift,
                "bc_boundary": DEPTH_BC_BOUNDARY + shift,
                "counts": counts,
            }
        )
    return {
        "split_rule": "event-date chronological 60/20/20; tuning is validation-only and verdict is test-only",
        "populations": populations,
        "bucket_sensitivity": bucket_sensitivity,
        "structure_failures": {"count": len(failures), "events": failures},
    }


def unavailable_envelope(*, start: date, end: date, reasons: list[str]) -> dict[str, Any]:
    return {
        "factor": {"id": FACTOR_ID, "version": FACTOR_VERSION, "name": FACTOR_NAME},
        "status": "unavailable",
        "unavailable_reasons": reasons,
        "request": {"start": start, "end": end},
        "provenance": {},
        "coverage": None,
        "research": None,
        "events": [],
        "promoted": PROMOTED,
    }


def evaluate_n_shape_pullback_depth(
    *,
    start: date,
    end: date,
    symbols: list[str] | None,
    reader: PublishedDailyMarketResearchReader | None,
    reversal_mode: str = DEFAULT_REVERSAL_MODE,
    reversal_value: float = DEFAULT_REVERSAL_VALUE,
    cost_bps: float = ROUND_TRIP_COST_BPS,
) -> dict[str, Any]:
    """Evaluate the causal pullback-depth factor against one pinned reader."""
    if start > end:
        raise ValueError("start must be <= end")
    if cost_bps < 0:
        raise ValueError("cost_bps must be non-negative")
    if reader is None:
        return unavailable_envelope(
            start=start, end=end, reasons=["n_shape_research_reader_missing"]
        )
    manifest = reader.manifest_sha256()
    if not _valid_manifest_sha256(manifest):
        return unavailable_envelope(
            start=start, end=end, reasons=["reader_manifest_identity_invalid"]
        )
    sources = reader.source_provenance()
    if not _valid_source_provenance(sources):
        return unavailable_envelope(start=start, end=end, reasons=["pit_source_provenance_invalid"])

    lookup_start = start - timedelta(days=CALENDAR_WARMUP_DAYS)
    lookup_end = end + timedelta(days=60)
    calendar = sorted(reader.market_days(lookup_start, lookup_end))
    if not calendar:
        return unavailable_envelope(start=start, end=end, reasons=["market_calendar_insufficient"])
    requested_symbols = (
        sorted({str(symbol) for symbol in reader.universe(start, end) if str(symbol)})
        if symbols is None
        else sorted({str(symbol) for symbol in symbols if str(symbol)})
    )
    events: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    censored: list[dict[str, Any]] = []
    zigzag_sensitivity: dict[str, int] = {}
    for symbol in requested_symbols:
        rows, censor = _bars_to_dicts(reader.daily_bars(symbol, lookup_start, lookup_end), symbol)
        if censor is not None:
            censored.append(censor)
            continue
        detected, broken = detect_causal_swings(
            symbol=symbol,
            rows=rows,
            calendar=calendar,
            event_window=(start, end),
            reversal_mode=reversal_mode,
            reversal_value=reversal_value,
            cost_bps=cost_bps,
        )
        events.extend(detected)
        failures.extend(broken)
        if reversal_mode == "fixed_pct":
            for value in ZIGZAG_SENSITIVITY_VALUES:
                sensitivity_events, _ = detect_causal_swings(
                    symbol=symbol,
                    rows=rows,
                    calendar=calendar,
                    event_window=(start, end),
                    reversal_mode="fixed_pct",
                    reversal_value=value,
                    cost_bps=cost_bps,
                )
                zigzag_sensitivity[str(value)] = zigzag_sensitivity.get(str(value), 0) + len(
                    sensitivity_events
                )

    events.sort(key=lambda event: (event["event_date"], event["symbol"]))
    failures.sort(key=lambda event: (event["failure_date"], event["symbol"]))
    payload = {
        "factor": {
            "id": FACTOR_ID,
            "version": FACTOR_VERSION,
            "name": FACTOR_NAME,
            "reachability": REACHABILITY,
        },
        "status": "ok",
        "unavailable_reasons": [],
        "request": {
            "start": start,
            "end": end,
            "reversal_mode": reversal_mode,
            "reversal_value": reversal_value,
            "cost_bps": cost_bps,
        },
        "provenance": {
            "reader": {
                "generation": reader.generation(),
                "manifest_sha256": manifest.lower(),
                "provider_id": reader.provider_id(),
            },
            "sources": sources,
            "factor_code": {
                "factor_id": FACTOR_ID,
                "version": FACTOR_VERSION,
                "definition": "docs/ISSUE-49/final-design.md",
                "causality": "confirmed pivots only; unconfirmed terminal leg emits no event",
            },
        },
        "coverage": {
            "symbols_total": len(requested_symbols),
            "events": len(events),
            "censored": len(censored),
            "structure_failures": len(failures),
        },
        "research": _research(events, failures),
        "sensitivity": {"zigzag_fixed_pct_event_counts": zigzag_sensitivity},
        "events": events,
        "censored": censored,
        "promoted": PROMOTED,
    }
    _validate_keys_no_trading_tokens(payload)
    return payload


__all__ = [
    "FACTOR_ID",
    "FACTOR_VERSION",
    "BUCKET_A",
    "BUCKET_B",
    "BUCKET_C",
    "PublishedDailyMarketResearchReader",
    "classify_depth_bucket",
    "detect_causal_swings",
    "evaluate_n_shape_pullback_depth",
    "resolve_n_shape_reader",
    "unavailable_envelope",
]
