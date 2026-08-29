"""Pure numerical helpers for retrieval-routing research."""

from __future__ import annotations

import numpy as np


def rankdata(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    i = 0
    while i < len(values):
        j = i + 1
        while j < len(values) and values[order[j]] == values[order[i]]:
            j += 1
        ranks[order[i:j]] = (i + j - 1) / 2.0 + 1.0
        i = j
    return ranks


def cross_sectional_rank_ic(scores: np.ndarray, returns: np.ndarray) -> tuple[float, int]:
    values: list[float] = []
    for s, r in zip(np.asarray(scores), np.asarray(returns)):
        mask = np.isfinite(s) & np.isfinite(r)
        if mask.sum() < 2:
            continue
        a, b = rankdata(s[mask]), rankdata(r[mask])
        if np.std(a) <= 0 or np.std(b) <= 0:
            continue
        values.append(float(np.corrcoef(a, b)[0, 1]))
    return (float(np.mean(values)) if values else 0.0, len(values))


def standardization_params(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    matrix = np.asarray(matrix, dtype=float)
    means = np.zeros(matrix.shape[1], dtype=float)
    stds = np.ones(matrix.shape[1], dtype=float)
    degenerate = np.zeros(matrix.shape[1], dtype=bool)
    for j in range(matrix.shape[1]):
        values = matrix[:, j][np.isfinite(matrix[:, j])]
        if len(values) < 2:
            degenerate[j] = True
            continue
        means[j] = float(np.mean(values))
        stds[j] = float(np.std(values))
        if stds[j] <= 1e-12:
            degenerate[j] = True
            stds[j] = 1.0
    return means, stds, degenerate


def apply_standardization(matrix: np.ndarray, means: np.ndarray, stds: np.ndarray) -> np.ndarray:
    return (np.asarray(matrix, dtype=float) - means.reshape(1, -1)) / stds.reshape(1, -1)


def quantile_edges(
    values: np.ndarray, probabilities: tuple[float, float] = (1 / 3, 2 / 3)
) -> np.ndarray:
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if not len(finite):
        raise ValueError("no finite values for quantile labels")
    return np.quantile(finite, probabilities).astype(float)


def digitize_labels(values: np.ndarray, edges: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    output = np.full(values.shape, -1, dtype=np.int8)
    mask = np.isfinite(values)
    output[mask] = np.searchsorted(edges, values[mask], side="right").astype(np.int8)
    return output


def normalized_entropy(labels: np.ndarray, classes: int = 3) -> float:
    labels = np.asarray(labels, dtype=int)
    counts = np.bincount(labels, minlength=classes).astype(float)
    total = counts.sum()
    if total <= 0 or classes <= 1:
        return 0.0
    p = counts[counts > 0] / total
    return float(-np.sum(p * np.log(p)) / np.log(classes))


def pairwise_distances(query: np.ndarray, library: np.ndarray, metric: str) -> np.ndarray:
    query, library = np.asarray(query, dtype=float), np.asarray(library, dtype=float)
    if metric == "euclidean":
        return np.sqrt(
            np.maximum(
                0.0,
                (query * query).sum(1, keepdims=True)
                + (library * library).sum(1)
                - 2 * query @ library.T,
            )
        )
    if metric == "cosine":
        qn = np.linalg.norm(query, axis=1, keepdims=True)
        ln = np.linalg.norm(library, axis=1, keepdims=True)
        return 1.0 - (query / np.where(qn == 0, 1.0, qn)) @ (library / np.where(ln == 0, 1.0, ln)).T
    raise ValueError(f"unsupported distance metric: {metric}")


def nearest_indices(distances: np.ndarray, k: int) -> np.ndarray:
    order = np.lexsort((np.arange(len(distances)), distances))
    if len(order) < k:
        raise ValueError("insufficient eligible neighbors")
    return order[:k]
