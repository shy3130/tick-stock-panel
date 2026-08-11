from __future__ import annotations

from datetime import date

from research.validation.run_structure_strategy_forward_watch_v1 import (
    MINIMUM_TRADING_DAYS,
    OBSERVATION_START,
    _validate_source,
    daily_attribution,
    frozen_protocol,
    readiness_gate,
    regime_attribution,
    turnover_proxy,
)


def _source():
    return {
        "version": "structure_strategy_replay_v1",
        "status": "complete",
        "evidence_status": "canonical_historical_replay_not_fresh_oos",
        "universe": {
            "seed": 20260723,
            "symbols": [f"{index:06d}.SZ" for index in range(400)],
        },
        "configurations": [
            {
                "key": "trend_always",
                "strategy_id": "trend_breakout",
                "regime_filter": None,
                "composition": None,
            },
            {
                "key": "pullback_always",
                "strategy_id": "pullback_to_support",
                "regime_filter": None,
                "composition": None,
            },
        ],
        "aggregate": {
            "trend_always": {"compounded_return": 0.1},
            "pullback_always": {"compounded_return": 0.2},
        },
    }


def test_registration_boundary_starts_after_registration_day():
    assert OBSERVATION_START == date(2026, 7, 30)


def test_protocol_freezes_source_universe_and_candidates():
    protocol = frozen_protocol(
        _source(),
        source_sha256="a" * 64,
        market_structure_protocol_hash="b" * 64,
    )

    assert protocol["universe"]["size"] == 400
    assert list(protocol["candidates"]) == ["trend_always", "pullback_always"]
    assert protocol["candidates"]["trend_always"]["execution"]["max_positions"] == 10
    assert protocol["readiness"]["auto_promote"] is False


def test_source_validation_rejects_universe_drift():
    source = _source()
    source["universe"]["symbols"][-1] = source["universe"]["symbols"][0]

    try:
        _validate_source(source)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate frozen universe must be rejected")


def test_readiness_never_auto_promotes():
    pending = readiness_gate(MINIMUM_TRADING_DAYS - 1)
    ready = readiness_gate(MINIMUM_TRADING_DAYS)

    assert pending["status"] == "PENDING_DATA"
    assert pending["remaining_to_minimum"] == 1
    assert ready["status"] == "READY_FOR_FROZEN_REVIEW"
    assert ready["auto_promote"] is False


def test_daily_and_regime_attribution_use_label_on_return_day():
    equity = [
        {"date": "2026-07-30", "value": 100.0},
        {"date": "2026-07-31", "value": 110.0},
        {"date": "2026-08-03", "value": 99.0},
    ]
    benchmark = [
        {"date": "2026-07-30", "close": 200.0},
        {"date": "2026-07-31", "close": 210.0},
        {"date": "2026-08-03", "close": 210.0},
    ]
    labels = {
        date(2026, 7, 30): "structural_bear",
        date(2026, 7, 31): "structural_bull",
        date(2026, 8, 3): "structural_bear",
    }

    rows = daily_attribution(equity, benchmark, labels)
    by_regime = regime_attribution(rows)

    assert [row["regime"] for row in rows] == ["structural_bull", "structural_bear"]
    assert round(rows[0]["strategy_return"], 6) == 0.1
    assert round(rows[0]["benchmark_return"], 6) == 0.05
    assert by_regime["structural_bull"]["strategy"]["attributed_days"] == 1
    assert by_regime["structural_bear"]["strategy"]["compounded_return"] == -0.1


def test_turnover_is_explicit_closed_trade_proxy():
    labels = {
        date(2026, 7, 31): "structural_bull",
        date(2026, 8, 3): "structural_bear",
    }
    trades = [
        {"entry_date": "2026-07-31", "entry_value": 20.0},
        {"entry_date": "2026-08-03", "entry_value": 30.0},
    ]
    curve = [
        {"date": "2026-07-31", "value": 100.0},
        {"date": "2026-08-03", "value": 100.0},
    ]

    result = turnover_proxy(trades, curve, labels, observed_days=2)

    assert result["one_way_turnover_proxy"] == 0.5
    assert result["entry_value_by_regime"]["structural_bull"] == 20.0
    assert result["entry_value_by_regime"]["structural_bear"] == 30.0
    assert "open-position" in result["limitation"]
