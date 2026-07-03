from __future__ import annotations

import numpy as np

METHODS = {"equal", "score_weight", "equal_vol", "risk_parity", "mean_variance", "max_diversification"}


def portfolio_weights(returns: np.ndarray, method: str, scores: np.ndarray | None = None) -> np.ndarray:
    n = int(returns.shape[1]) if returns.ndim == 2 and returns.size else 0
    if n <= 0:
        return np.array([], dtype=float)
    if method == "score_weight" and scores is not None:
        raw = np.maximum(np.asarray(scores, dtype=float), 0.0)
        if raw.sum() > 0:
            return raw / raw.sum()
    if method == "equal":
        return np.repeat(1 / n, n)

    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r).all(axis=1)]
    if r.shape[0] < 2:
        return np.repeat(1 / n, n)
    cov = np.cov(r, rowvar=False)
    cov = np.atleast_2d(cov) + np.eye(n) * 1e-8
    vol = np.sqrt(np.maximum(np.diag(cov), 1e-12))

    if method == "equal_vol":
        return _normalize(1 / vol, n)
    if method == "risk_parity":
        return _risk_parity(cov, n)
    if method == "mean_variance":
        mu = np.maximum(r.mean(axis=0), 0.0)
        return _normalize(np.linalg.pinv(cov) @ mu, n)
    if method == "max_diversification":
        return _normalize(np.linalg.pinv(cov) @ vol, n)
    return np.repeat(1 / n, n)


def _risk_parity(cov: np.ndarray, n: int) -> np.ndarray:
    w = np.repeat(1 / n, n)
    b = np.repeat(1 / n, n)
    for _ in range(200):
        sw = cov @ w
        port_var = float(w @ sw)
        if port_var <= 0:
            return np.repeat(1 / n, n)
        for i in range(n):
            alpha = float(cov[i, i])
            if alpha <= 0:
                continue
            beta = float(sw[i] - cov[i, i] * w[i])
            disc = beta * beta + 4 * alpha * b[i] * port_var
            w[i] = (-beta + np.sqrt(max(disc, 0.0))) / (2 * alpha)
            sw = cov @ w
        w = _normalize(w, n)
    return w


def _normalize(raw: np.ndarray, n: int) -> np.ndarray:
    raw = np.maximum(np.asarray(raw, dtype=float), 0.0)
    total = float(raw.sum())
    if not np.isfinite(total) or total <= 0:
        return np.repeat(1 / n, n)
    return raw / total
