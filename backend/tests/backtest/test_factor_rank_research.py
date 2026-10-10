from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest
from fastapi import HTTPException

from app.api import screener as screener_api
from app.api import strategy as strategy_api
from app.backtest.matrix import build_market_data_matrix, validate_signal_matrix
from app.backtest.optimizer import expand_param_grid
from app.backtest.strategy import StrategyDependencyResolver
from app.strategy.engine import StrategyEngine

STRATEGY_PATH = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "strategy"
    / "research"
    / "factor_rank_research.py"
)


def _market():
    panel = pl.DataFrame({
        "symbol": ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"] * 2,
        "date": [date(2024, 1, 2)] * 4 + [date(2024, 1, 3)] * 4,
        "open": [10.0] * 8,
        "high": [10.5] * 8,
        "low": [9.5] * 8,
        "close": [10.0] * 8,
        "volume": [1_000.0] * 8,
        "amount": [1.0, 2.0, 3.0, 4.0, 4.0, 3.0, 2.0, 1.0],
        "turnover_rate": [4.0, 3.0, 2.0, 1.0, 1.0, 2.0, 3.0, 4.0],
    })
    return build_market_data_matrix(
        panel,
        field_columns={"amount", "turnover_rate"},
    )


def test_strategy_loads_as_builtin_matrix_native_and_grid_params_validate():
    strategy = StrategyEngine._load_file(STRATEGY_PATH)

    assert strategy.meta["id"] == "factor_rank_research"
    assert strategy.meta["research_only"] is True
    assert strategy.execution_backend == "matrix_native"
    assert strategy.matrix_strategy is not None
    assert strategy.meta["scoring"] == {}
    combos = expand_param_grid(
        strategy.meta["params"],
        {
            "entry_score": [50.0, 75.0],
            "exit_score": [20.0],
            "top_rank": [1, 2],
        },
    )
    assert len(combos) == 4
    assert strategy.matrix_strategy.required_warmup_bars({}) == 60
    assert {"amount", "turnover_rate", "close"}.issubset(
        strategy.matrix_strategy.required_fields()
    )


def test_research_template_is_hidden_from_ordinary_strategy_apis(tmp_path):
    engine = StrategyEngine(strategy_dirs=[STRATEGY_PATH.parent])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(strategy_engine=engine, repo=repo))
    )

    screener_payload = screener_api.strategies(request)
    strategy_payload = strategy_api.list_strategies(request)

    assert engine.has("factor_rank_research")
    assert "factor_rank_research" in {
        item["id"] for item in engine.list_strategies(include_research=True)
    }
    assert "factor_rank_research" not in {
        item["id"] for item in screener_payload["presets"]
    }
    assert "factor_rank_research" not in {
        item["id"] for item in strategy_payload["strategies"]
    }

    with pytest.raises(HTTPException) as screener_error:
        screener_api.run_preset(
            screener_api.PresetRequest(
                strategy_id="factor_rank_research",
                as_of=date(2024, 1, 2),
            ),
            request,
        )
    assert screener_error.value.status_code == 404

    with pytest.raises(HTTPException) as strategy_error:
        strategy_api.run_strategy(
            strategy_api.RunRequest(
                strategy_id="factor_rank_research",
                as_of=date(2024, 1, 2),
            ),
            request,
        )
    assert strategy_error.value.status_code == 404


def test_dependency_resolver_includes_parameter_scoring_fields():
    strategy = StrategyEngine._load_file(STRATEGY_PATH)

    plan = StrategyDependencyResolver().resolve(
        strategy,
        params={"scoring": {"amount": 1.0, "ma20_bias": 1.0}},
        basic_filter={"enabled": False},
        entry_signals=strategy.entry_signals,
        exit_signals=strategy.exit_signals,
    )

    assert {"amount", "close"}.issubset(plan.base_columns)
    assert plan.indicator_columns == frozenset()
    assert {"amount", "close"}.issubset(plan.matrix_columns)


def test_strategy_uses_controlled_scoring_directions_thresholds_and_top_rank():
    strategy = StrategyEngine._load_file(STRATEGY_PATH).matrix_strategy
    market = _market()

    signals = strategy.compute_signals(
        market,
        {
            "scoring": {"amount": 1.0, "turnover_rate": 1.0},
            "directions": {"amount": "high", "turnover_rate": "low"},
            "entry_score": 60.0,
            "exit_score": 25.0,
            "top_rank": 1,
        },
    )

    validate_signal_matrix(signals, market.shape)
    assert signals.entry.sum(axis=1).tolist() == [1, 1]
    assert signals.entry.tolist() == [[0, 0, 0, 1], [1, 0, 0, 0]]
    assert signals.exit.tolist() == [[1, 0, 0, 0], [0, 0, 0, 1]]
    assert signals.entry_signal_ids == ("signal_factor_rank_entry",)
    assert signals.exit_signal_ids == ("signal_factor_rank_exit",)
    assert not signals.score.flags.writeable


def test_strategy_direction_changes_score_without_dynamic_formula_execution():
    strategy = StrategyEngine._load_file(STRATEGY_PATH).matrix_strategy
    market = _market()

    high = strategy.compute_signals(
        market,
        {
            "scoring": {"amount": 1.0},
            "directions": {"amount": "high"},
            "entry_score": 0.0,
            "exit_score": 0.0,
            "top_rank": 4,
        },
    )
    low = strategy.compute_signals(
        market,
        {
            "scoring": {"amount": 1.0},
            "directions": {"amount": "low"},
            "entry_score": 0.0,
            "exit_score": 0.0,
            "top_rank": 4,
        },
    )

    np.testing.assert_allclose(high.score + low.score, 100.0)
    with pytest.raises(ValueError, match="unsupported matrix feature"):
        strategy.compute_signals(
            market,
            {
                "scoring": {"__import__('os').system('bad')": 1.0},
                "entry_score": 50.0,
                "exit_score": 20.0,
                "top_rank": 1,
            },
        )


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"scoring": {}}, "non-empty scoring"),
        (
            {"scoring": {f"factor_{index}": 1.0 for index in range(5)}},
            "at most 4 factors",
        ),
        (
            {
                "scoring": {"amount": 1.0},
                "directions": {"turnover_rate": "low"},
            },
            "absent from scoring",
        ),
        (
            {
                "scoring": {"amount": 1.0},
                "entry_score": 20.0,
                "exit_score": 30.0,
            },
            "exit_score must not exceed",
        ),
    ],
)
def test_strategy_rejects_uncontrolled_or_invalid_research_params(params, message):
    strategy = StrategyEngine._load_file(STRATEGY_PATH).matrix_strategy

    with pytest.raises(ValueError, match=message):
        strategy.compute_signals(_market(), params)
