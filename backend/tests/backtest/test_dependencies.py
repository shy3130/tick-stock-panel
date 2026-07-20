from __future__ import annotations

import polars as pl

from app.backtest.strategy import StrategyDependencyResolver
from app.strategy.engine import StrategyDef


def _strategy(**overrides) -> StrategyDef:
    values = dict(
        meta={"id": "deps", "scoring": {"momentum_20d": 1.0}, "order_by": "score"},
        basic_filter={"enabled": False},
        entry_signals=["signal_macd_golden"],
        exit_signals=["signal_ma20_breakdown"],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        alerts=[],
        filter_fn=lambda df, params: pl.col("rsi_14") < params["rsi_max"],
        filter_history_fn=None,
        lookback_days=20,
        source="builtin",
    )
    values.update(overrides)
    return StrategyDef(**values)


def test_resolver_merges_signals_scoring_filter_and_execution_columns():
    plan = StrategyDependencyResolver().resolve(
        _strategy(),
        params={"rsi_max": 30},
        basic_filter={"enabled": False},
        entry_signals=["signal_macd_golden"],
        exit_signals=["signal_ma20_breakdown"],
    )

    assert {"macd_dif", "macd_dea", "ma20", "momentum_20d", "rsi_14"} <= set(plan.indicator_columns)
    assert {"signal_macd_golden", "signal_ma20_breakdown", "signal_limit_up", "signal_limit_down"} <= set(plan.signal_columns)
    assert {"symbol", "date", "open", "high", "low", "close", "volume", "raw_close", "raw_high"} <= set(plan.base_columns)
    assert "raw_low" not in plan.base_columns
    assert "rsi_6" not in plan.indicator_columns
    assert plan.full_feature_fallback is False


def test_resolver_expands_virtual_scoring_dependencies():
    strategy = _strategy(meta={
        "id": "deps",
        "scoring": {"ma20_bias": 0.6, "vol_ratio_5d": 0.4},
        "order_by": "score",
    })

    plan = StrategyDependencyResolver().resolve(
        strategy,
        params={"rsi_max": 30},
        basic_filter={"enabled": False},
        entry_signals=[],
        exit_signals=[],
    )

    assert {"ma20", "vol_ratio_5d"} <= set(plan.indicator_columns)
    assert "close" in plan.base_columns
    assert "ma20_bias" not in plan.base_columns
    assert "ma20_bias" not in plan.indicator_columns


def test_history_strategy_without_required_features_falls_back_to_full(caplog):
    strategy = _strategy(
        filter_fn=None,
        filter_history_fn=lambda df, params: df,
        required_features=frozenset(),
        source="custom",
    )

    plan = StrategyDependencyResolver().resolve(
        strategy,
        params={},
        basic_filter={"enabled": False},
        entry_signals=[],
        exit_signals=[],
    )

    assert plan.full_feature_fallback is True
    assert "rsi_14" in plan.indicator_columns
    assert "falls back to full feature computation" in caplog.text


def test_history_strategy_required_features_avoids_fallback():
    strategy = _strategy(
        filter_fn=None,
        filter_history_fn=lambda df, params: df,
        required_features=frozenset({"ma20", "momentum_20d"}),
        source="custom",
    )

    plan = StrategyDependencyResolver().resolve(
        strategy,
        params={},
        basic_filter={"enabled": False},
        entry_signals=[],
        exit_signals=[],
    )

    assert plan.full_feature_fallback is False
    assert {"ma20", "momentum_20d"} <= set(plan.indicator_columns)
    assert "rsi_14" not in plan.indicator_columns


def test_matrix_native_resolves_raw_fields_and_protocol_warmup_without_indicators():
    class NativeStrategy:
        def required_fields(self):
            return frozenset({"open", "high", "low", "close", "volume"})

        def required_warmup_bars(self, params):
            return 120

        def compute_signals(self, market, params):  # pragma: no cover - resolver only
            raise AssertionError

    strategy = _strategy(
        filter_fn=None,
        filter_history_fn=None,
        execution_backend="matrix_native",
        matrix_strategy=NativeStrategy(),
        required_features=frozenset(),
    )
    plan = StrategyDependencyResolver().resolve(
        strategy,
        params={},
        basic_filter={"enabled": True, "amount_min": 100.0},
        entry_signals=[],
        exit_signals=[],
    )

    assert plan.execution_backend == "matrix_native"
    assert plan.indicator_columns == frozenset()
    assert {"open", "high", "low", "close", "volume", "amount"} <= set(plan.base_columns)
    assert plan.warmup_bars == 120
    assert plan.full_feature_fallback is False
