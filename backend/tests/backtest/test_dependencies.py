from __future__ import annotations

import types
from datetime import date, timedelta

import polars as pl
import pytest

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
        filter_fn=lambda df, params: pl.col("rsi_14") < params["rsi_max"],
        filter_history_fn=None,
        lookback_days=20,
        source="builtin",
    )
    values.update(overrides)
    return StrategyDef(**values)


@pytest.fixture(autouse=True)
def _migrated_signal_definitions(monkeypatch):
    """注入迁移自内置的信号定义 (信号列定义驱动, 不依赖运行目录)。"""
    from tests.signal_seeds import install_pipeline_caches
    install_pipeline_caches(monkeypatch)


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
    # 涨跌停信号族统一加载不复权三价 (翘板用 raw_low 判定"曾触及跌停")
    assert "raw_low" in plan.base_columns
    assert "rsi_6" not in plan.indicator_columns
    assert plan.full_feature_fallback is False


def test_resolver_includes_raw_low_for_limit_signal_family():
    """回归: 翘板信号 (signal_limit_down_recovery) 依赖不复权 raw_low。

    旧 bug: 涨跌停基础列只声明 raw_close/raw_high, panel 加载缺 raw_low,
    因子用到翘板信号时 compute_limit_signals 抛
    "unable to find column raw_low" → 回测特征准备失败。
    """
    plan = StrategyDependencyResolver().resolve(
        _strategy(),
        params={"rsi_max": 30},
        basic_filter={"enabled": False},
        entry_signals=["signal_limit_down_recovery"],
        exit_signals=[],
    )

    assert "signal_limit_down_recovery" in plan.signal_columns
    assert {"raw_close", "raw_high", "raw_low"} <= set(plan.base_columns)


def test_load_panel_for_backtest_supplies_raw_low_for_recovery(monkeypatch, tmp_path):
    """回归 (端到端): resolver → load_panel_for_backtest → compute_limit_signals 全链路。

    旧 bug: 涨跌停基础列漏 raw_low, 因子用到翘板信号时回测特征准备抛
    "unable to find column raw_low"。
    """
    from app.backtest.engine import BacktestEngine

    n_days = 40
    start = date(2024, 1, 1)
    dates = [start + timedelta(days=i) for i in range(n_days)]
    px = [10.0 + 0.01 * i for i in range(n_days)]
    panel_lf = pl.LazyFrame({
        "symbol": ["600001.SH"] * n_days,
        "date": dates,
        "open": px, "high": px, "low": px, "close": px,
        "volume": [100_000.0] * n_days,
        "amount": [1_000_000.0] * n_days,
        "raw_close": px, "raw_high": px, "raw_low": px,
    })
    monkeypatch.setattr("app.backtest.engine.pl.scan_parquet", lambda path, *a, **k: panel_lf)

    instruments = pl.DataFrame({
        "symbol": ["600001.SH"], "name": ["普通股"],
        "limit_up": [11.0], "limit_down": [9.0],
    })
    repo = types.SimpleNamespace(
        store=types.SimpleNamespace(data_dir=tmp_path),
        get_enriched_range=lambda *a, **k: None,
        get_instruments_asset=lambda at: instruments,
        get_historical_shares=lambda: pl.DataFrame(),
    )
    plan = StrategyDependencyResolver().resolve(
        _strategy(),
        params={"rsi_max": 30},
        basic_filter={"enabled": False},
        entry_signals=["signal_limit_down_recovery"],
        exit_signals=[],
    )

    df = BacktestEngine(repo).load_panel_for_backtest(
        ["600001.SH"], start, dates[-1], plan, asset_type="stock",
    )

    assert "raw_low" in df.columns
    assert "signal_limit_down_recovery" in df.columns


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


def test_resolver_honors_full_scoring_replacement():
    plan = StrategyDependencyResolver().resolve(
        _strategy(),
        params={"rsi_max": 30},
        basic_filter={"enabled": False},
        entry_signals=[],
        exit_signals=[],
        overrides={
            "scoring": {"amount_ratio_5d": 1.0},
            "scoring_replace": True,
        },
    )

    assert "amount" in plan.base_columns
    assert "momentum_20d" not in plan.indicator_columns


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
