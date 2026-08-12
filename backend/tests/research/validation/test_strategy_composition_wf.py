from __future__ import annotations

from research.validation.run_strategy_composition_wf import (
    CONFIGS,
    MOM_TREND,
    _canonical_protocol,
    _composition,
    _protocol_hash,
    aggregate,
    contrasts,
    promotion_gate,
)


def test_fixed_composition_contract_uses_and_and_locked_weights():
    composition = _composition("bullish_alignment")
    assert composition == {
        "entry_mode": "and",
        "score_mode": "weighted_rank",
        "components": [
            {"strategy_id": "bullish_alignment", "weight": 0.3},
            {
                "strategy_id": "custom_factor",
                "weight": 0.7,
                "params": {"factor_formula": MOM_TREND},
            },
        ],
    }


def test_protocol_hash_is_deterministic_and_records_leakage_boundary():
    periods = [{"phase": "historical_replay", "fold": "F1"}]
    first = _canonical_protocol(["A", "B"], periods)
    second = _canonical_protocol(["A", "B"], periods)
    assert _protocol_hash(first) == _protocol_hash(second)
    assert "not fresh OOS" in first["leakage_notice"]
    assert first["configs"] == list(CONFIGS)


def test_aggregate_and_contrasts_keep_failed_runs_out():
    records = [
        {
            "phase": "historical_replay",
            "fold": "F1",
            "config_key": config["key"],
            "total_return": 0.10 if "factor" in config["key"] else 0.05,
            "sharpe": 1.0 if "factor" in config["key"] else 0.5,
            "max_drawdown": -0.1,
            "n_trades": 3,
        }
        for config in CONFIGS
    ]
    records.append({
        "phase": "historical_replay",
        "fold": "F2",
        "config_key": "bullish_factor_30_70",
        "error": "explicit failure",
    })

    summary = aggregate(records, "historical_replay")
    assert summary["bullish_factor_30_70"]["n_periods"] == 1
    result = contrasts(summary)
    assert (
        result["bullish_factor_minus_base"]["delta"]["mean_total_return"]
        == 0.05
    )


def test_promotion_gate_never_promotes_an_undersized_fresh_window():
    gate = promotion_gate([
        {"phase": "historical_replay", "trading_days": 999},
        {"phase": "unseen_observation", "trading_days": 15},
    ])
    assert gate["status"] == "PENDING_DATA"
    assert gate["observed_fresh_trading_days"] == 15
    assert gate["remaining_to_minimum"] == 45
    assert gate["auto_promote"] is False
