"""Symbol-clustered statistics; selection/paired algorithms are shared."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

from app.services.hold_firm_patterns.models import (
    BOOTSTRAP_ROUNDS,
    BOOTSTRAP_SEED,
    CI_LOWER_QUANTILE,
    CI_UPPER_QUANTILE,
    MIN_VALID_BOOTSTRAP_REPLICATES,
)
from app.services.hold_firm_patterns.statistics import (
    BootstrapResult,
    _quantile,
    gates,
    paired_cluster_bootstrap,
    selection_cluster_bootstrap,
)


def interaction_cluster_bootstrap(
    high_qualified: Mapping[str, Sequence[float]],
    high_not_selected: Mapping[str, Sequence[float]],
    low_qualified: Mapping[str, Sequence[float]],
    low_not_selected: Mapping[str, Sequence[float]],
    *,
    seed: int = BOOTSTRAP_SEED,
    rounds: int = BOOTSTRAP_ROUNDS,
    min_valid: int = MIN_VALID_BOOTSTRAP_REPLICATES,
) -> BootstrapResult:
    symbols = sorted(
        set(high_qualified) | set(high_not_selected) | set(low_qualified) | set(low_not_selected)
    )
    if not symbols:
        return BootstrapResult(None, None, None, 0, rounds)
    cells = {
        s: tuple(
            (sum(float(x) for x in group), len(group))
            for group in (
                high_qualified.get(s, ()),
                high_not_selected.get(s, ()),
                low_qualified.get(s, ()),
                low_not_selected.get(s, ()),
            )
        )
        for s in symbols
    }
    rng = random.Random(seed)
    values = []
    for _ in range(rounds):
        sums = [0.0, 0, 0.0, 0, 0.0, 0, 0.0, 0]
        for s in rng.choices(symbols, k=len(symbols)):
            cell = cells[s]
            for i, (total, count) in enumerate(cell):
                sums[2 * i] += total
                sums[2 * i + 1] += count
        if all(sums[i] for i in (1, 3, 5, 7)):
            values.append(
                (sums[0] / sums[1] - sums[2] / sums[3]) - (sums[4] / sums[5] - sums[6] / sums[7])
            )
    if not values:
        return BootstrapResult(None, None, None, 0, rounds)
    return BootstrapResult(
        sum(values) / len(values),
        _quantile(values, CI_LOWER_QUANTILE),
        _quantile(values, CI_UPPER_QUANTILE),
        len(values),
        rounds,
    )


__all__ = [
    "BootstrapResult",
    "gates",
    "interaction_cluster_bootstrap",
    "paired_cluster_bootstrap",
    "selection_cluster_bootstrap",
]
