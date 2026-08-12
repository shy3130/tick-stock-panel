from __future__ import annotations

from copy import deepcopy

from research.optimization.run_core_strategy_walkforward_v1 import (
    SPECS,
    V1_STRATEGY_IDS,
    aggregate_stats,
    compare_to_default,
    default_fold_record,
    next_frozen_params,
    protocol_hash,
)


def _fold(index, params, total_return, max_drawdown=-0.1, n_trades=20):
    return {
        "index": index,
        "best_params": params,
        "oos_stats": {
            "total_return": total_return,
            "max_drawdown": max_drawdown,
            "sharpe": 1.0,
            "sortino": 1.2,
            "calmar": 1.5,
            "win_rate": 0.4,
            "n_trades": n_trades,
        },
    }


def _default(index, total_return, max_drawdown=-0.1, n_trades=20):
    return {
        "index": index,
        "stats": {
            "total_return": total_return,
            "max_drawdown": max_drawdown,
            "sharpe": 0.5,
            "sortino": 0.6,
            "calmar": 0.8,
            "win_rate": 0.3,
            "n_trades": n_trades,
        },
    }


def test_specs_cover_exactly_the_five_core_strategies():
    assert set(SPECS) == set(V1_STRATEGY_IDS)


def test_next_frozen_params_uses_train_winners_not_oos_metrics():
    folds = [
        _fold(0, {"threshold": 1}, 0.1),
        _fold(1, {"threshold": 1}, -0.5),
        _fold(2, {"threshold": 2}, 0.9),
    ]
    first = next_frozen_params(folds, {"enabled": True})
    changed = deepcopy(folds)
    for fold in changed:
        fold["oos_stats"]["total_return"] *= -100
    second = next_frozen_params(changed, {"enabled": True})
    assert first == second
    assert first["params"] == {"enabled": True, "threshold": 1}


def test_compare_gate_accepts_only_consistent_improvement():
    optimized = [_fold(i, {"threshold": 1}, 0.10, n_trades=20) for i in range(5)]
    defaults = [_default(i, 0.02, n_trades=20) for i in range(5)]
    result = compare_to_default(optimized, defaults, planned_folds=5)
    assert result["status"] == "CANDIDATE_FOR_FUTURE_FROZEN_OOS"
    assert result["auto_apply"] is False
    assert all(result["conditions"].values())

    optimized[0]["oos_stats"]["total_return"] = -0.8
    rejected = compare_to_default(optimized, defaults, planned_folds=5)
    assert rejected["status"] == "REJECTED_HISTORICAL_REPLAY"


def test_aggregate_stats_compounds_returns():
    result = aggregate_stats([
        {"stats": _default(0, 0.10)["stats"]},
        {"stats": _default(1, -0.05)["stats"]},
    ])
    assert result["compounded_return"] == 0.045
    assert result["positive_fold_ratio"] == 0.5


def test_protocol_hash_is_order_stable():
    first = {"a": 1, "b": {"x": 2}}
    second = {"b": {"x": 2}, "a": 1}
    assert protocol_hash(first) == protocol_hash(second)


def test_no_signal_default_fold_is_explicit_cash_not_dropped():
    record = default_fold_record(3, {"error": "在指定区间内未产生买入信号"})
    assert record["status"] == "no_signal_cash"
    assert record["stats"]["total_return"] == 0.0
    assert record["stats"]["n_trades"] == 0

    failure = default_fold_record(3, {"error": "data corrupt"})
    assert failure == {"index": 3, "error": "data corrupt"}
