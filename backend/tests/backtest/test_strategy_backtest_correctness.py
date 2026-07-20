from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine, SimResult
from app.backtest.matrix import build_market_data_matrix, make_signal_matrix, rolling_mean
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.strategy.engine import StrategyDef


def _strategy(**kwargs) -> StrategyDef:
    defaults = dict(
        meta={"id": "test", "name": "test", "scoring": {}, "params": [], "limit": 100},
        basic_filter={"enabled": True, "amount_min": 100.0},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        alerts=[],
        filter_fn=lambda df, params: pl.lit(True),
        filter_history_fn=None,
        lookback_days=1,
        source="custom",
        file_path=None,
    )
    defaults.update(kwargs)
    return StrategyDef(**defaults)


class _StrategyEngineStub:
    def __init__(self, strategy: StrategyDef) -> None:
        self.strategy = strategy

    def get(self, strategy_id: str) -> StrategyDef:
        return self.strategy


class _RepoStub:
    def get_index_daily(self, *args, **kwargs) -> pl.DataFrame:
        return pl.DataFrame()


class _EngineStub:
    def __init__(self, panel: pl.DataFrame) -> None:
        self.panel = panel
        self.repo = _RepoStub()
        self.load_args = None
        self.load_count = 0
        self.sim_panel: pl.DataFrame | None = None
        self.sim_matrix = None
        self.sim_entries: pl.Series | None = None

    def load_panel(self, symbols, start: date, end: date, columns=None, asset_type: str = "stock") -> pl.DataFrame:
        self.load_count += 1
        self.load_args = (symbols, start, end)
        self.load_asset_type = asset_type
        return self.panel

    def load_panel_for_backtest(self, symbols, start, end, feature_plan, asset_type="stock") -> pl.DataFrame:
        return self.load_panel(symbols, start, end, columns=sorted(feature_plan.base_columns), asset_type=asset_type)

    def load_market_data_matrix_for_backtest(
        self,
        symbols,
        start,
        end,
        feature_plan,
        asset_type="stock",
        **kwargs,
    ):
        panel = self.load_panel_for_backtest(
            symbols,
            start,
            end,
            feature_plan,
            asset_type=asset_type,
        )
        field_columns = (
            set(feature_plan.base_columns)
            | set(feature_plan.instrument_columns)
            | set(feature_plan.matrix_columns)
        )
        return build_market_data_matrix(panel, field_columns=field_columns)

    def simulate_portfolio(self, panel, entries, exits, config, progress_cb=None, cancel_event=None, entry_signal_ids=None, exit_signal_ids=None) -> SimResult:
        self.sim_panel = panel
        self.sim_entries = entries
        return SimResult(
            equity_curve=[{"date": "2024-01-01", "value": config.initial_capital}],
            drawdown_curve=[{"date": "2024-01-01", "value": 0.0}],
            trades=[],
            per_symbol_stats=[],
            stats={"total_return": 0.0, "n_trades": 0},
        )

    def simulate_market_matrix(
        self,
        matrix,
        config,
        progress_cb=None,
        cancel_event=None,
        options=None,
    ) -> SimResult:
        self.sim_matrix = matrix
        return SimResult(
            equity_curve=[{"date": "2024-01-01", "value": config.initial_capital}],
            drawdown_curve=[{"date": "2024-01-01", "value": 0.0}],
            trades=[],
            per_symbol_stats=[],
            stats={"total_return": 0.0, "n_trades": 0},
        )


def test_basic_filter_only_limits_entries_not_panel_rows():
    start = date(2024, 1, 1)
    rows = []
    for i, amount in enumerate([1000.0, 0.0, 1000.0]):
        rows.append({
            "symbol": "A",
            "name": "A",
            "date": start + timedelta(days=i),
            "open": 10.0 + i,
            "high": 10.0 + i,
            "low": 10.0 + i,
            "close": 10.0 + i,
            "volume": 100_000,
            "amount": amount,
            "signal_limit_up": False,
            "signal_limit_down": False,
        })
    panel = pl.DataFrame(rows).sort(["symbol", "date"])
    engine = _EngineStub(panel)
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub(_strategy()))

    result = service.run(StrategyBacktestConfig(
        strategy_id="test",
        symbols=None,
        start=start,
        end=start + timedelta(days=2),
        matching="close_t",
        mode="position",
    ))

    assert result.error is None
    assert engine.sim_matrix is not None
    assert engine.sim_matrix.shape == (3, 1)
    assert engine.sim_matrix.entry[:, 0].tolist() == [1, 0, 1]
    assert engine.load_args is not None
    assert engine.load_args[1] < start  # warmup 只用于计算, 不参与正式交易
    assert result.stats["selection"] == {
        "strategy_matches": 2,
        "entry_candidates": 2,
        "entry_trigger_filtered": 0,
        "entry_trigger_enabled": False,
    }


def test_selection_stats_explain_entry_trigger_filtering():
    start = date(2024, 1, 1)
    panel = pl.DataFrame([
        {
            "symbol": symbol,
            "name": symbol,
            "date": start,
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 1000.0,
            "amount": 1000.0,
            "signal_limit_up": symbol == "A",
            "signal_limit_down": False,
        }
        for symbol in ("A", "B")
    ]).sort(["symbol", "date"])
    engine = _EngineStub(panel)
    service = StrategyBacktestService(
        engine=engine,
        strategy_engine=_StrategyEngineStub(
            _strategy(entry_signals=["signal_limit_up"]),
        ),
    )

    result = service.run(StrategyBacktestConfig(
        strategy_id="test",
        symbols=None,
        start=start,
        end=start,
        matching="close_t",
        mode="position",
    ))

    assert result.error is None
    assert result.stats["selection"] == {
        "strategy_matches": 2,
        "entry_candidates": 1,
        "entry_trigger_filtered": 1,
        "entry_trigger_enabled": True,
    }


def test_score_normalizes_inside_strategy_candidate_universe():
    panel = pl.DataFrame({
        "symbol": ["A", "B", "C"],
        "date": [date(2024, 1, 1)] * 3,
        "factor": [10.0, 20.0, 1000.0],
    })
    universe = pl.Series([True, True, False], dtype=pl.Boolean)
    strategy = SimpleNamespace(meta={"scoring": {"factor": 1.0}, "order_by": "score", "descending": True})

    scored = StrategyBacktestService._apply_score(panel, strategy, None, universe_mask=universe)
    scores = dict(zip(scored["symbol"].to_list(), scored["score"].to_list()))

    assert scores["A"] == 0.0
    assert scores["B"] == 100.0
    assert scores["C"] == 0.0


def test_full_mode_executes_every_candidate_with_strategy_rules():
    start = date(2024, 1, 1)
    panel = pl.DataFrame([
        {"symbol": "A", "name": "A", "date": start, "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1, "amount": 1000.0, "signal_limit_up": False, "signal_limit_down": False},
        {"symbol": "A", "name": "A", "date": start + timedelta(days=1), "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1, "amount": 0.0, "signal_limit_up": False, "signal_limit_down": False},
        {"symbol": "A", "name": "A", "date": start + timedelta(days=2), "open": 20.0, "high": 20.0, "low": 20.0, "close": 20.0, "volume": 1, "amount": 1000.0, "signal_limit_up": False, "signal_limit_down": False},
    ]).sort(["symbol", "date"])

    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]
    engine.load_panel_for_backtest = lambda symbols, s, e, plan, asset_type="stock": panel  # type: ignore[method-assign]
    strategy = _strategy(
        filter_fn=lambda df, params: pl.col("date") == start,
        max_hold_days=1,
    )
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub(strategy))

    result = service.run(StrategyBacktestConfig(
        strategy_id="test",
        symbols=None,
        start=start,
        end=start,
        mode="full",
        matching="open_t+1",
        fees_pct=0,
        slippage_bps=0,
        holding_days=1,
    ))

    assert result.error is None
    assert result.stats["full_kind"] == "candidate_execution"
    assert result.stats["n_candidates"] == 1
    assert result.stats["n_trades"] == 1
    assert result.trades[0]["entry_date"] == str(start + timedelta(days=1))
    assert result.trades[0]["exit_reason"] == "max_hold"
    assert result.stats["avg_return"] == round(20 / 11 - 1, 4)


def test_matrix_native_strategy_uses_shared_orchestrator_path():
    start = date(2024, 1, 1)
    panel = pl.DataFrame([
        {"symbol": "A", "name": "A", "date": start, "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1.0, "amount": 1000.0, "raw_close": 10.0, "raw_high": 10.0, "signal_limit_up": False, "signal_limit_down": False},
        {"symbol": "A", "name": "A", "date": start + timedelta(days=1), "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1.0, "amount": 1000.0, "raw_close": 11.0, "raw_high": 11.0, "signal_limit_up": False, "signal_limit_down": False},
    ])

    class NativeStrategy:
        def required_fields(self):
            return frozenset({"open", "high", "low", "close", "volume"})

        def required_warmup_bars(self, params):
            return 1

        def compute_signals(self, market, params):
            return make_signal_matrix(
                market.shape,
                entry=np.ones(market.shape, dtype=np.uint8),
            )

    engine = _EngineStub(panel)
    strategy = _strategy(
        meta={"id": "native", "name": "native", "scoring": {}, "params": [], "limit": 100},
        basic_filter={"enabled": True, "amount_min": 100.0},
        filter_fn=None,
        execution_backend="matrix_native",
        matrix_strategy=NativeStrategy(),
        required_features=frozenset({"amount"}),
    )
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub(strategy))

    result = service.run(StrategyBacktestConfig(
        strategy_id="native",
        symbols=None,
        start=start,
        end=start + timedelta(days=1),
        matching="close_t",
        mode="position",
    ))

    assert result.error is None
    assert engine.sim_matrix is not None
    assert engine.sim_matrix.entry[:, 0].tolist() == [1, 1]
    assert result.stats["execution_backend"] == "matrix_native"
    assert result.stats["selection"] == {
        "strategy_matches": 2,
        "entry_candidates": 2,
        "entry_trigger_filtered": 0,
        "entry_trigger_enabled": False,
    }


def test_matrix_native_accepts_legacy_default_signal_overrides_but_rejects_replacements():
    start = date(2024, 1, 1)
    panel = pl.DataFrame([
        {"symbol": "A", "name": "A", "date": start, "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1.0, "amount": 1000.0, "raw_close": 10.0, "raw_high": 10.0, "signal_limit_up": False, "signal_limit_down": False},
        {"symbol": "A", "name": "A", "date": start + timedelta(days=1), "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1.0, "amount": 1000.0, "raw_close": 11.0, "raw_high": 11.0, "signal_limit_up": False, "signal_limit_down": False},
    ])

    class NativeStrategy:
        def required_fields(self):
            return frozenset({"open", "high", "low", "close", "volume"})

        def required_warmup_bars(self, params):
            return 1

        def compute_signals(self, market, params):
            return make_signal_matrix(market.shape, entry=np.ones(market.shape, dtype=np.uint8))

    engine = _EngineStub(panel)
    strategy = _strategy(
        meta={"id": "native_defaults", "name": "native", "scoring": {}, "params": [], "limit": 100},
        filter_fn=None,
        execution_backend="matrix_native",
        matrix_strategy=NativeStrategy(),
        entry_signals=["signal_custom_entry"],
        exit_signals=["signal_custom_exit"],
        required_features=frozenset(),
    )
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub(strategy))

    accepted = service.run(StrategyBacktestConfig(
        strategy_id="native_defaults",
        symbols=None,
        start=start,
        end=start + timedelta(days=1),
        matching="close_t",
        overrides={
            "entry_signals": ["signal_custom_entry"],
            "exit_signals": ["signal_custom_exit"],
        },
    ))
    assert accepted.error is None

    rejected = service.run(StrategyBacktestConfig(
        strategy_id="native_defaults",
        symbols=None,
        start=start,
        end=start + timedelta(days=1),
        matching="close_t",
        overrides={"entry_signals": ["signal_other"]},
    ))
    assert rejected.error == "matrix_native 策略的进出场信号由策略协议生成，不支持列信号覆盖"


def test_matrix_optimizer_preparation_loads_and_builds_base_data_once():
    start = date(2024, 1, 1)
    panel = pl.DataFrame([
        {"symbol": "A", "name": "A", "date": start, "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1.0, "amount": 1000.0, "raw_close": 10.0, "raw_high": 10.0, "signal_limit_up": False, "signal_limit_down": False},
        {"symbol": "A", "name": "A", "date": start + timedelta(days=1), "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1.0, "amount": 1000.0, "raw_close": 11.0, "raw_high": 11.0, "signal_limit_up": False, "signal_limit_down": False},
    ])

    class NativeStrategy:
        def required_fields(self):
            return frozenset({"open", "high", "low", "close", "volume"})

        def required_warmup_bars(self, params):
            return int(params.get("warmup", 1))

        def compute_signals(self, market, params):
            return make_signal_matrix(
                market.shape,
                entry=np.ones(market.shape, dtype=np.uint8),
            )

    engine = _EngineStub(panel)
    strategy = _strategy(
        meta={
            "id": "native",
            "name": "native",
            "scoring": {},
            "params": [{"id": "warmup", "type": "int", "default": 1, "min": 1, "max": 10}],
            "limit": 100,
        },
        basic_filter={"enabled": False},
        filter_fn=None,
        execution_backend="matrix_native",
        matrix_strategy=NativeStrategy(),
        required_features=frozenset({"amount"}),
    )
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub(strategy))
    configs = [
        StrategyBacktestConfig(
            strategy_id="native",
            symbols=None,
            start=start,
            end=start + timedelta(days=1),
            params={"warmup": warmup},
            matching="close_t",
        )
        for warmup in (1, 10)
    ]

    prepared = service.prepare_matrix_optimization(configs)
    results = [service.run(config, prepared=prepared) for config in configs]

    assert engine.load_count == 1
    assert prepared.market_data.nbytes > 0
    assert all(result.error is None for result in results)
    assert all(result.stats["shared_market_data"] is True for result in results)
    assert all(result.stats["shared_market_data_bytes"] == prepared.market_data.nbytes for result in results)


def test_matrix_cache_preserves_trades_daily_equity_and_core_stats():
    start = date(2024, 1, 1)
    panel = pl.DataFrame([
        {
            "symbol": "A",
            "name": "A",
            "date": start + timedelta(days=offset),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1000.0,
            "amount": close * 1000.0,
            "raw_close": close,
            "raw_high": close,
            "signal_limit_up": False,
            "signal_limit_down": False,
        }
        for offset, close in enumerate((10.0, 11.0, 12.0, 11.0, 13.0, 12.0))
    ])

    class RollingEntry:
        def required_fields(self):
            return frozenset({"close"})

        def required_warmup_bars(self, params):
            return 2

        def compute_signals(self, market, params):
            entry = market.close >= rolling_mean(market.close, 2)
            return make_signal_matrix(market.shape, entry=entry.astype(np.uint8))

    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]
    engine.load_market_data_matrix_for_backtest = (  # type: ignore[method-assign]
        lambda symbols, s, e, plan, asset_type="stock", **kwargs: build_market_data_matrix(
            panel,
            field_columns=(
                set(plan.base_columns)
                | set(plan.instrument_columns)
                | set(plan.matrix_columns)
            ),
        )
    )
    strategy = _strategy(
        meta={"id": "rolling", "name": "rolling", "scoring": {}, "params": [], "limit": 100},
        basic_filter={"enabled": False},
        filter_fn=None,
        execution_backend="matrix_native",
        matrix_strategy=RollingEntry(),
        required_features=frozenset({"amount"}),
        max_hold_days=1,
    )
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub(strategy))
    config = StrategyBacktestConfig(
        strategy_id="rolling",
        symbols=None,
        start=start,
        end=start + timedelta(days=5),
        matching="close_t",
        fees_pct=0,
        slippage_bps=0,
        max_positions=1,
    )

    uncached = service.run(config)
    prepared = service.prepare_matrix_optimization([config])
    cached = service.run(config, prepared=prepared)
    cached_again = service.run(config, prepared=prepared)
    prepared.compute_cache.close()

    assert uncached.error is None
    assert cached.error is None
    assert cached_again.error is None
    assert cached.trades == uncached.trades
    assert cached.equity_curve == uncached.equity_curve
    assert cached.drawdown_curve == uncached.drawdown_curve
    for name in (
        "total_return",
        "annual_return",
        "max_drawdown",
        "sharpe",
        "sortino",
        "n_trades",
    ):
        assert cached.stats[name] == uncached.stats[name]
        assert cached_again.stats[name] == uncached.stats[name]
    assert cached_again.stats["matrix_compute_cache"]["hits"] > 0
