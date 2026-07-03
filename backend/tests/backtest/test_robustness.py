import numpy as np

from app.backtest import robustness as rb


def test_bootstrap_ci_deterministic_with_seed():
    rets = np.random.default_rng(1).normal(0.001, 0.01, 300)
    a = rb.bootstrap_sharpe_ci(rets, n_boot=200, seed=7)
    b = rb.bootstrap_sharpe_ci(rets, n_boot=200, seed=7)
    assert a == b
    assert a["ci_low"] < a["sharpe"] < a["ci_high"]


def test_permutation_pvalue_low_for_strong_signal():
    rets = np.random.default_rng(3).normal(0.002, 0.005, 400)
    assert rb.mc_permutation_pvalue(rets, n_perm=500, seed=3)["p_value"] < 0.05


def test_exit_reason_breakdown():
    rows = rb.exit_reason_breakdown([
        {"exit_reason": "stop_loss", "pnl_pct": -5.0},
        {"exit_reason": "stop_loss", "pnl_pct": -4.0},
        {"exit_reason": "signal", "pnl_pct": 8.0},
        {"exit_reason": None, "pnl_pct": 1.0},
    ])
    by = {r["exit_reason"]: r for r in rows}
    assert by["stop_loss"]["n"] == 2
    assert by["stop_loss"]["win_rate"] == 0.0
    assert by["signal"]["avg_pnl_pct"] == 8.0
    assert by["(none)"]["n"] == 1


def test_walk_forward_summary_dispersion():
    s = rb.walk_forward_summary([{"stats": {"sharpe": 1.0}}, {"stats": {"sharpe": 1.2}}, {"stats": {"sharpe": -0.3}}])
    assert s["n_folds"] == 3
    assert s["positive_folds"] == 2
    assert abs(s["mean"] - 0.6333) < 1e-3
    assert s["worst"] == -0.3
