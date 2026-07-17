from datetime import date

import polars as pl

from app.strategy.engine import StrategyDataContext, StrategyDef, StrategyEngine


def _make_engine() -> tuple[StrategyEngine, StrategyDataContext]:
    df = pl.DataFrame({"symbol": ["A", "B", "C"], "value": [1, 2, 3]})
    engine = StrategyEngine(strategy_dirs=[])
    engine._strategies["saved_params"] = StrategyDef(
        meta={"id": "saved_params", "scoring": {}, "limit": 100},
        basic_filter={"enabled": False},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        alerts=[],
        filter_fn=lambda _df, params: pl.col("value") >= params.get("min_value", 1),
        filter_history_fn=None,
        lookback_days=1,
        source="custom",
    )
    return engine, StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=date(2026, 7, 15),
        current=df,
    )


def test_run_applies_saved_strategy_params():
    engine, context = _make_engine()
    result = engine.run(
        "saved_params",
        context,
        overrides={"params": {"min_value": 2}},
    )

    assert [row["symbol"] for row in result.rows] == ["B", "C"]


def test_explicit_params_override_saved_strategy_params():
    engine, context = _make_engine()
    result = engine.run(
        "saved_params",
        context,
        params={"min_value": 3},
        overrides={"params": {"min_value": 2}},
    )

    assert [row["symbol"] for row in result.rows] == ["C"]
