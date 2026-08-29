"""Fixed-seed placebo primitives; no I/O or global randomness."""

from __future__ import annotations

import numpy as np


def random_neighbor_indices(rng: np.random.Generator, pool_size: int, k: int) -> np.ndarray:
    if pool_size < k:
        raise ValueError("insufficient placebo neighbor pool")
    return rng.choice(pool_size, size=k, replace=False)


def permuted_labels(rng: np.random.Generator, labels: np.ndarray) -> np.ndarray:
    return np.asarray(labels)[rng.permutation(len(labels))]


def placebo_summary(
    real: float, values: np.ndarray, quantile: float = 0.95
) -> tuple[bool, float, float]:
    values = np.asarray(values, dtype=float)
    if not len(values):
        return True, 0.0, 0.0
    q = float(np.quantile(values, quantile))
    return bool(real <= q), float(np.mean(values)), q
