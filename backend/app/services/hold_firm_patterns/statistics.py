"""Deterministic, symbol-clustered research statistics for Issue #38."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Mapping, Sequence

from app.services.hold_firm_patterns.models import (
    BOOTSTRAP_ROUNDS,
    BOOTSTRAP_SEED,
    CI_LOWER_QUANTILE,
    CI_UPPER_QUANTILE,
    MIN_VALID_BOOTSTRAP_REPLICATES,
)


@dataclass(frozen=True)
class BootstrapResult:
    mean_difference: float | None
    lower: float | None
    upper: float | None
    valid_replicates: int
    rounds: int = BOOTSTRAP_ROUNDS


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(float(x) for x in values)
    if not ordered:
        raise ValueError("quantile requires values")
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def selection_cluster_bootstrap(
    qualified: Mapping[str, Sequence[float]],
    not_selected: Mapping[str, Sequence[float]],
    *,
    seed: int = BOOTSTRAP_SEED,
    rounds: int = BOOTSTRAP_ROUNDS,
    min_valid: int = MIN_VALID_BOOTSTRAP_REPLICATES,
) -> BootstrapResult:
    """Resample the union of symbols, retaining each cluster's all events."""
    symbols = sorted(set(qualified) | set(not_selected))
    if not symbols:
        return BootstrapResult(None, None, None, 0, rounds)
    per_symbol = {
        s: (
            sum(float(v) for v in qualified.get(s, ())),
            len(qualified.get(s, ())),
            sum(float(v) for v in not_selected.get(s, ())),
            len(not_selected.get(s, ())),
        )
        for s in symbols
    }
    rng = random.Random(seed)
    differences: list[float] = []
    for _ in range(rounds):
        sums = [0.0, 0, 0.0, 0]
        for symbol in rng.choices(symbols, k=len(symbols)):
            qsum, qn, nsum, nn = per_symbol[symbol]
            sums[0] += qsum
            sums[1] += qn
            sums[2] += nsum
            sums[3] += nn
        if sums[1] and sums[3]:
            differences.append(sums[0] / sums[1] - sums[2] / sums[3])
    if not differences:
        return BootstrapResult(None, None, None, 0, rounds)
    return BootstrapResult(
        sum(differences) / len(differences),
        _quantile(differences, CI_LOWER_QUANTILE),
        _quantile(differences, CI_UPPER_QUANTILE),
        len(differences),
        rounds,
    )


def paired_cluster_bootstrap(
    pairs: Mapping[str, tuple[str, float, float]],
    *,
    seed: int = BOOTSTRAP_SEED,
    rounds: int = BOOTSTRAP_ROUNDS,
    min_valid: int = MIN_VALID_BOOTSTRAP_REPLICATES,
) -> tuple[BootstrapResult, BootstrapResult]:
    """Resample qualified-symbol clusters for paired return and MAE deltas."""
    by_symbol: dict[str, list[tuple[float, float]]] = {}
    for symbol, d_return, d_mae in pairs.values():
        by_symbol.setdefault(symbol, []).append((float(d_return), float(d_mae)))
    symbols = sorted(by_symbol)
    if not symbols:
        empty = BootstrapResult(None, None, None, 0, rounds)
        return empty, empty
    rng = random.Random(seed)
    returns: list[float] = []
    maes: list[float] = []
    for _ in range(rounds):
        selected = rng.choices(symbols, k=len(symbols))
        vals = [pair for symbol in selected for pair in by_symbol[symbol]]
        if vals:
            returns.append(sum(v[0] for v in vals) / len(vals))
            maes.append(sum(v[1] for v in vals) / len(vals))

    def result(values: list[float]) -> BootstrapResult:
        if not values:
            return BootstrapResult(None, None, None, 0, rounds)
        return BootstrapResult(
            sum(values) / len(values),
            _quantile(values, CI_LOWER_QUANTILE),
            _quantile(values, CI_UPPER_QUANTILE),
            len(values),
            rounds,
        )

    return result(returns), result(maes)


def gates(events: int, symbols: int, *, min_events: int = 30, min_symbols: int = 10) -> bool:
    return events >= min_events and symbols >= min_symbols
