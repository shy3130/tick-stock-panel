"""Deterministic population statistics and cluster bootstrap verdicts."""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from datetime import date
from statistics import mean

from .benchmark import EqualWeightBenchmark, attribution_layers, layer_status
from .models import (
    BOOTSTRAP_ROUNDS,
    BOOTSTRAP_SEED,
    COST_BPS,
    ENTRY_VARIANTS,
    FORWARD_HORIZONS,
    MIN_OOS_EVENTS,
    MIN_OOS_SYMBOLS,
    MIN_VALID_BOOTSTRAP_REPLICATES,
    OOS_START,
    POLE_CONDITIONS,
    THETA1_GRID,
    THETA2_GRID,
)


def split_of(day: date, oos_start: date = OOS_START) -> str:
    return "is" if day < oos_start else "oos"


def _stats(samples, calendar_index, benchmark, horizon, cost_bps):
    key = f"forward_{horizon}d_return"
    valid = [s for s in samples if s.get("forward", {}).get(key) is not None]
    values = []
    excess = []
    for s in valid:
        idx = calendar_index.get(s["confirm_date"])
        if idx is None:
            continue
        b = benchmark.forward_return(s["confirm_date"], horizon)
        values.append(float(s["forward"][key]))
        if b is not None:
            excess.append(float(s["forward"][key]) - b)
    n = len(values)
    result = {
        "n_raw": len(valid),
        "n_sample": n,
        "clusters": n,
        "censored_forward": len(samples) - len(valid),
        "status": "ok" if n >= MIN_OOS_EVENTS else "insufficient_sample",
        "mean": None,
        "median": None,
        "std": None,
        "win_rate": None,
        "ci95_low": None,
        "ci95_high": None,
        "post_cost_mean": None,
        "excess_mean": None,
        "excess_post_cost_mean": None,
    }
    if n:
        m = mean(values)
        sd = math.sqrt(mean([(x - m) ** 2 for x in values]))
        half = 1.96 * sd / math.sqrt(n)
        result.update(
            mean=m,
            median=statistics.median(values),
            std=sd,
            win_rate=sum(x > 0 for x in values) / n,
            ci95_low=m - half,
            ci95_high=m + half,
            post_cost_mean=m - cost_bps / 10000,
        )
    if excess:
        em = mean(excess)
        result.update(excess_mean=em, excess_post_cost_mean=em - cost_bps / 10000)
    return result


def _index_attribution(events, index_benchmark) -> dict:
    if index_benchmark is None:
        return {
            "status": "unavailable",
            "reason": "index_layer_not_sealed",
            "horizons": {},
        }
    horizons = {}
    for horizon in FORWARD_HORIZONS:
        key = f"forward_{horizon}d_return"
        per_event = []
        for event in events:
            raw = event.get("forward", {}).get(key)
            if raw is None:
                continue
            index_return = index_benchmark.forward_return(event["confirm_date"], horizon)
            if index_return is None:
                continue
            raw = float(raw)
            per_event.append(
                {
                    "symbol": event["symbol"],
                    "confirm_date": event["confirm_date"].isoformat(),
                    "forward_return": raw,
                    "index_return": index_return,
                    "excess": raw - index_return,
                }
            )
        values = [item["excess"] for item in per_event]
        horizons[str(horizon)] = {
            "n": len(per_event),
            "events": len(events),
            "mean": mean(values) if values else None,
            "per_event": per_event,
        }
    return {
        "status": "ok",
        "code": index_benchmark.code,
        "pin": index_benchmark.pin,
        "horizons": horizons,
    }


def _bootstrap(values: dict[str, list[float]]) -> dict[str, object]:
    symbols = sorted(values)
    rounds = BOOTSTRAP_ROUNDS
    if not symbols:
        return {
            "mean": None,
            "lower": None,
            "upper": None,
            "valid_replicates": 0,
            "rounds": rounds,
            "seed": BOOTSTRAP_SEED,
        }
    rng = random.Random(BOOTSTRAP_SEED)
    reps = []
    for _ in range(rounds):
        chosen = rng.choices(symbols, k=len(symbols))
        flat = [x for s in chosen for x in values[s]]
        if flat:
            reps.append(sum(flat) / len(flat))
    ordered = sorted(reps)

    def q(p):
        return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p))]

    return {
        "mean": mean(reps),
        "lower": q(0.025),
        "upper": q(0.975),
        "valid_replicates": len(reps),
        "rounds": rounds,
        "seed": BOOTSTRAP_SEED,
    }


def _oos_folds(selected, oos_start):
    ordered = sorted(
        (
            e
            for e in selected
            if e.get("confirm_date") is not None and split_of(e["confirm_date"], oos_start) == "oos"
        ),
        key=lambda e: (e["confirm_date"], e["symbol"]),
    )
    dates = sorted({e["confirm_date"] for e in ordered})
    if not dates:
        return []
    step = max(1, (len(dates) + 2) // 3)
    return [
        {
            "start": min(chunk).isoformat(),
            "end": max(chunk).isoformat(),
            "events": [e for e in ordered if e["confirm_date"] in set(chunk)],
        }
        for i in range(0, len(dates), step)
        if (chunk := dates[i : i + step])
    ]


def _walk_forward(selected, calendar, benchmark, oos_start, cost_bps):
    index = {d: i for i, d in enumerate(calendar)}
    folds = _oos_folds(selected, oos_start)
    return {
        "n_folds": len(folds),
        "folds": [
            {
                "start": fold["start"],
                "end": fold["end"],
                "count": len(fold["events"]),
                "symbols": len({e["symbol"] for e in fold["events"]}),
                "stats_by_horizon": {
                    h: _stats(fold["events"], index, benchmark, h, cost_bps)
                    for h in FORWARD_HORIZONS
                },
            }
            for fold in folds
        ],
    }


def _gate_reasons(samples, boot, label="oos_events"):
    reasons = []
    if len(samples) < MIN_OOS_EVENTS:
        reasons.append(f"{label}_below_minimum")
    if len({e["symbol"] for e in samples}) < MIN_OOS_SYMBOLS:
        reasons.append(f"{label}_symbols_below_minimum")
    if int(boot["valid_replicates"]) < MIN_VALID_BOOTSTRAP_REPLICATES:
        reasons.append("bootstrap_insufficient_replicates")
    return reasons


def _verdict(reasons, boot):
    if reasons:
        return "unavailable"
    return "accepted" if boot["lower"] is not None and boot["lower"] > 0 else "rejected"


def _net_by_symbol(samples, benchmark, cost_bps):
    out = defaultdict(list)
    for event in samples:
        raw = event.get("forward", {}).get("forward_63d_return")
        base = benchmark.forward_return(event["confirm_date"], 63)
        if raw is not None and base is not None:
            out[event["symbol"]].append(float(raw) - base - cost_bps / 10000)
    return out


def _event_arm(samples, calendar, benchmark, oos_start, cost_bps, poles):
    index = {d: i for i, d in enumerate(calendar)}
    boot = _bootstrap(_net_by_symbol(samples, benchmark, cost_bps))
    reasons = _gate_reasons(samples, boot)
    metrics = {}
    for horizon in FORWARD_HORIZONS:
        stats = _stats(samples, index, benchmark, horizon, cost_bps)
        metrics[f"{horizon // 21}m_raw"] = stats["mean"]
        metrics[f"{horizon // 21}m_net"] = stats["excess_post_cost_mean"]
        metrics[f"{horizon // 21}m_n"] = stats["n_sample"]
        metrics[f"{horizon // 21}m_censored"] = stats["censored_forward"]
    metrics.update(
        {
            "n_samples": len(samples),
            "symbols": len({e["symbol"] for e in samples}),
            "trigger_rate": len(samples) / poles if poles else None,
            "cost_bps": cost_bps,
        }
    )
    return {
        "verdict": _verdict(reasons, boot),
        "verdict_reasons": reasons,
        "metrics": metrics,
        "bootstrap": boot,
        "walk_forward": _walk_forward(samples, calendar, benchmark, oos_start, cost_bps),
    }


def _f3_result(records, oos_start):
    if records is None:
        return {
            "verdict": "unavailable",
            "verdict_reasons": ["failure_records_unavailable"],
            "metrics": {},
        }
    oos = [
        r
        for r in records
        if r.get("failure_week") is not None and split_of(r["failure_week"], oos_start) == "oos"
    ]
    by_symbol = defaultdict(list)
    for record in oos:
        by_symbol[record["symbol"]].append(1.0 if record.get("re_established") else 0.0)
    boot = _bootstrap(by_symbol)
    reasons = _gate_reasons(oos, boot, "oos_failures")
    weeks = sorted({r["failure_week"] for r in oos})
    step = max(1, (len(weeks) + 2) // 3) if weeks else 1
    folds = []
    for i in range(0, len(weeks), step):
        chunk = weeks[i : i + step]
        sub = [r for r in oos if r["failure_week"] in set(chunk)]
        folds.append(
            {
                "start": min(chunk).isoformat(),
                "end": max(chunk).isoformat(),
                "failures": len(sub),
                "re_established": sum(bool(r.get("re_established")) for r in sub),
            }
        )
    count = sum(bool(r.get("re_established")) for r in oos)
    return {
        "verdict": _verdict(reasons, boot),
        "verdict_reasons": reasons,
        "metrics": {
            "failures": len(oos),
            "re_established": count,
            "re_establishment_rate": count / len(oos) if oos else None,
            "symbols": len({r["symbol"] for r in oos}),
        },
        "bootstrap": boot,
        "walk_forward": {"n_folds": len(folds), "folds": folds},
    }


def _delta_bootstrap(strict_by, loose_by):
    symbols = sorted(set(strict_by) | set(loose_by))
    if not symbols:
        return {
            "mean": None,
            "lower": None,
            "upper": None,
            "valid_replicates": 0,
            "rounds": BOOTSTRAP_ROUNDS,
            "seed": BOOTSTRAP_SEED,
        }
    rng = random.Random(BOOTSTRAP_SEED)
    reps = []
    for _ in range(BOOTSTRAP_ROUNDS):
        chosen = rng.choices(symbols, k=len(symbols))
        left = [x for s in chosen for x in strict_by.get(s, [])]
        right = [x for s in chosen for x in loose_by.get(s, [])]
        if left and right:
            reps.append(mean(left) - mean(right))
    if not reps:
        return {
            "mean": None,
            "lower": None,
            "upper": None,
            "valid_replicates": 0,
            "rounds": BOOTSTRAP_ROUNDS,
            "seed": BOOTSTRAP_SEED,
        }
    ordered = sorted(reps)

    def q(percentile: float) -> float:
        return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * percentile))]

    return {
        "mean": mean(reps),
        "lower": q(0.025),
        "upper": q(0.975),
        "valid_replicates": len(reps),
        "rounds": BOOTSTRAP_ROUNDS,
        "seed": BOOTSTRAP_SEED,
    }


def _f4_arms(events, calendar, benchmark, oos_start, cost_bps):
    arms = {}
    for variant in ENTRY_VARIANTS:
        loose = [
            e
            for e in events
            if variant in e.get("variants", []) and split_of(e["confirm_date"], oos_start) == "oos"
        ]
        strict = [e for e in loose if e.get("strict_limit_up") is True]
        strict_by = _net_by_symbol(strict, benchmark, cost_bps)
        loose_by = _net_by_symbol(loose, benchmark, cost_bps)
        boot = _delta_bootstrap(strict_by, loose_by)
        reasons = []
        if len(strict) < MIN_OOS_EVENTS:
            reasons.append("strict_oos_events_below_minimum")
        if len({e["symbol"] for e in strict}) < MIN_OOS_SYMBOLS:
            reasons.append("strict_oos_symbols_below_minimum")
        if len(loose) < MIN_OOS_EVENTS:
            reasons.append("loose_oos_events_below_minimum")
        if len({e["symbol"] for e in loose}) < MIN_OOS_SYMBOLS:
            reasons.append("loose_oos_symbols_below_minimum")
        if int(boot["valid_replicates"]) < MIN_VALID_BOOTSTRAP_REPLICATES:
            reasons.append("bootstrap_insufficient_replicates")
        by_theta = {}
        for t1 in THETA1_GRID:
            for t2 in THETA2_GRID:
                subset = [
                    e
                    for e in loose
                    if e.get("cum_gain", 1) <= t1 + 1e-9 and e.get("flag_depth", 1) <= t2 + 1e-9
                ]
                subset_strict = [e for e in subset if e.get("strict_limit_up") is True]
                sn = _net_by_symbol(subset_strict, benchmark, cost_bps)
                ln = _net_by_symbol(subset, benchmark, cost_bps)
                sv = [x for values in sn.values() for x in values]
                lv = [x for values in ln.values() for x in values]
                sm = mean(sv) if sv else None
                lm = mean(lv) if lv else None
                by_theta[f"t1_{t1:.3f}__t2_{t2:.3f}"] = {
                    "strict_n": len(subset_strict),
                    "loose_n": len(subset),
                    "strict_net_3m": sm,
                    "loose_net_3m": lm,
                    "delta": sm - lm if sm is not None and lm is not None else None,
                }
        sv = [x for values in strict_by.values() for x in values]
        lv = [x for values in loose_by.values() for x in values]
        delta = mean(sv) - mean(lv) if sv and lv else None
        arms[variant] = {
            "verdict": _verdict(reasons, boot),
            "verdict_reasons": reasons,
            "metrics": {
                "strict_n": len(strict),
                "loose_n": len(loose),
                "symbols_strict": len(strict_by),
                "symbols_loose": len(loose_by),
                "strict_net_3m": mean(sv) if sv else None,
                "loose_net_3m": mean(lv) if lv else None,
                "delta_net_3m": delta,
                "cost_bps": cost_bps,
            },
            "bootstrap": boot,
            "by_theta": by_theta,
        }
    return arms


def _factor_verdicts(events, calendar, benchmark, *, oos_start, cost_bps, diagnostics=None):
    diagnostics = diagnostics or {}
    oos = [
        e
        for e in events
        if e.get("confirm_date") is not None and split_of(e["confirm_date"], oos_start) == "oos"
    ]
    f2 = {
        variant: _event_arm(
            [e for e in oos if variant in e.get("variants", [])],
            calendar,
            benchmark,
            oos_start,
            cost_bps,
            diagnostics.get("poles"),
        )
        for variant in ENTRY_VARIANTS
    }
    return {
        "F1": _event_arm(oos, calendar, benchmark, oos_start, cost_bps, diagnostics.get("poles")),
        "F2": {"arms": f2},
        "F3": _f3_result(diagnostics.get("failure_records"), oos_start),
        "F4": {"arms": _f4_arms(events, calendar, benchmark, oos_start, cost_bps)},
    }


def build_research_layer(
    events,
    calendar: list[date],
    benchmark: EqualWeightBenchmark,
    *,
    oos_start: date = OOS_START,
    cost_bps: float = COST_BPS,
    diagnostics: dict | None = None,
    source_provenance: dict | None = None,
    index_benchmark=None,
) -> dict:
    index = {d: i for i, d in enumerate(calendar)}
    cells = {}
    for variant in ENTRY_VARIANTS:
        for condition in POLE_CONDITIONS:
            for theta1 in THETA1_GRID:
                for theta2 in THETA2_GRID:
                    selected = [
                        e
                        for e in events
                        if variant in e.get("variants", [])
                        and e.get("cum_gain", 1) > 0
                        and e.get("cum_gain", 1) <= theta1 + 1e-9
                        and e.get("flag_depth", 1) <= theta2 + 1e-9
                        and (condition == "loose" or e.get("strict_limit_up") is True)
                    ]
                    splits = {}
                    for split in ("is", "oos"):
                        subset = [
                            e for e in selected if split_of(e["confirm_date"], oos_start) == split
                        ]
                        splits[split] = {
                            "count_raw": len(subset),
                            "symbols": len({e["symbol"] for e in subset}),
                            "stats_by_horizon": {
                                h: _stats(subset, index, benchmark, h, cost_bps)
                                for h in FORWARD_HORIZONS
                            },
                        }
                    oos = [e for e in selected if split_of(e["confirm_date"], oos_start) == "oos"]
                    by_symbol = defaultdict(list)
                    for e in oos:
                        raw = e.get("forward", {}).get("forward_63d_return")
                        b = benchmark.forward_return(e["confirm_date"], 63)
                        if raw is not None and b is not None:
                            by_symbol[e["symbol"]].append(raw - b - cost_bps / 10000)
                    boot = _bootstrap(by_symbol)
                    reasons = []
                    if len(oos) < MIN_OOS_EVENTS:
                        reasons.append("oos_events_below_minimum")
                    if len({e["symbol"] for e in oos}) < MIN_OOS_SYMBOLS:
                        reasons.append("oos_symbols_below_minimum")
                    if int(boot["valid_replicates"]) < MIN_VALID_BOOTSTRAP_REPLICATES:
                        reasons.append("bootstrap_insufficient_replicates")
                    verdict = (
                        "unavailable"
                        if reasons
                        else (
                            "accepted"
                            if boot["lower"] is not None and boot["lower"] > 0
                            else "rejected"
                        )
                    )
                    key = f"{variant}__{condition}__t1_{theta1:.3f}__t2_{theta2:.3f}"
                    cells[key] = {
                        "variant": variant,
                        "condition": condition,
                        "theta1": theta1,
                        "theta2": theta2,
                        "is": splits["is"],
                        "oos": splits["oos"],
                        "bootstrap": boot,
                        "verdict": verdict,
                    }
    return {
        "design": {
            "return_price_scale": "canonical_adjusted",
            "cost_bps": cost_bps,
            "min_oos_events": MIN_OOS_EVENTS,
            "min_oos_symbols": MIN_OOS_SYMBOLS,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "bootstrap_rounds": BOOTSTRAP_ROUNDS,
            "trigger_rate_denominator": "pole_runs_total",
            "f3_caveat": "failure records without a complete 13-week window are conservatively counted as not re-established",
        },
        "benchmark_layers": layer_status(index_benchmark),
        "attribution_layers": attribution_layers(source_provenance or {}, index_benchmark),
        "market_index_attribution": _index_attribution(events, index_benchmark),
        "cells": cells,
        "factor_verdicts": _factor_verdicts(
            events,
            calendar,
            benchmark,
            oos_start=oos_start,
            cost_bps=cost_bps,
            diagnostics=diagnostics,
        ),
    }
