"""Backtest robustness checks: pure post-processing."""
from __future__ import annotations

import numpy as np

_ANNUAL = 252.0


def returns_from_equity_curve(curve: list[dict]) -> np.ndarray:
    vals = np.asarray([float(p["value"]) for p in curve], dtype=float)
    if len(vals) < 2:
        return np.empty(0)
    return vals[1:] / vals[:-1] - 1.0


def bootstrap_sharpe_ci(rets, n_boot: int = 1000, ci: float = 0.95, seed: int | None = None) -> dict:
    rets = np.asarray(rets, dtype=float)
    if len(rets) == 0:
        return {"sharpe": 0.0, "ci_low": 0.0, "ci_high": 0.0, "ci": ci, "n_boot": n_boot}
    rng = np.random.default_rng(seed)
    samples = np.empty(n_boot)
    for i in range(n_boot):
        samples[i] = _sharpe(rets[rng.integers(0, len(rets), len(rets))])
    lo, hi = np.quantile(samples, [(1 - ci) / 2, 1 - (1 - ci) / 2])
    return {"sharpe": round(_sharpe(rets), 4), "ci_low": round(float(lo), 4), "ci_high": round(float(hi), 4), "ci": ci, "n_boot": n_boot}


def mc_permutation_pvalue(rets, n_perm: int = 1000, seed: int | None = None) -> dict:
    rets = np.asarray(rets, dtype=float)
    if len(rets) == 0:
        return {"p_value": 1.0, "n_perm": n_perm, "observed_sharpe": 0.0}
    rng = np.random.default_rng(seed)
    observed = abs(_sharpe(rets))
    count = 0
    for _ in range(n_perm):
        if abs(_sharpe(rets * rng.choice([-1.0, 1.0], size=len(rets)))) >= observed:
            count += 1
    return {"p_value": round((count + 1) / (n_perm + 1), 4), "n_perm": n_perm, "observed_sharpe": round(_sharpe(rets), 4)}


def exit_reason_breakdown(trades: list[dict]) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for trade in trades:
        groups.setdefault(str(trade.get("exit_reason") or "(none)"), []).append(float(trade.get("pnl_pct") or 0.0))
    rows = []
    for reason, pnls in sorted(groups.items()):
        arr = np.asarray(pnls, dtype=float)
        rows.append({
            "exit_reason": reason,
            "n": int(len(arr)),
            "win_rate": round(float((arr > 0).mean()), 4),
            "avg_pnl_pct": round(float(arr.mean()), 4),
            "total_pnl_pct": round(float(arr.sum()), 4),
        })
    return rows


def walk_forward_summary(folds: list[dict], metric: str = "sharpe") -> dict:
    vals = np.asarray([float((f.get("stats") or {}).get(metric, 0.0)) for f in folds], dtype=float)
    if len(vals) == 0:
        return {"metric": metric, "n_folds": 0, "mean": 0.0, "std": 0.0, "worst": 0.0, "positive_folds": 0}
    return {
        "metric": metric,
        "n_folds": int(len(vals)),
        "mean": round(float(vals.mean()), 4),
        "std": round(float(vals.std(ddof=1)), 4) if len(vals) > 1 else 0.0,
        "worst": round(float(vals.min()), 4),
        "positive_folds": int((vals > 0).sum()),
    }


def _sharpe(rets: np.ndarray) -> float:
    if len(rets) < 2:
        return 0.0
    sd = rets.std(ddof=1)
    return 0.0 if sd == 0 else float(rets.mean() / sd * np.sqrt(_ANNUAL))
