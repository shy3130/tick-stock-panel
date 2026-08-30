"""Deterministic population statistics and cluster bootstrap verdicts."""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from datetime import date
from statistics import mean

from .benchmark import EqualWeightBenchmark, layer_status
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
    key = f"forward_{horizon}d_raw_return"
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


def build_research_layer(
    events,
    calendar: list[date],
    benchmark: EqualWeightBenchmark,
    *,
    oos_start: date = OOS_START,
    cost_bps: float = COST_BPS,
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
                        raw = e.get("forward", {}).get("forward_63d_raw_return")
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
                        "verdict_reasons": reasons,
                    }
    return {
        "design": {
            "oos_start": oos_start.isoformat(),
            "horizons": list(FORWARD_HORIZONS),
            "cost_bps": cost_bps,
            "min_oos_events": MIN_OOS_EVENTS,
            "min_oos_symbols": MIN_OOS_SYMBOLS,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "bootstrap_rounds": BOOTSTRAP_ROUNDS,
        },
        "benchmark_layers": layer_status(),
        "cells": cells,
    }
