from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl

from app.backtest.engine import BacktestEngine, SimResult
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
        self.sim_panel: pl.DataFrame | None = None
        self.sim_entries: pl.Series | None = None

    def load_panel(self, symbols, start: date, end: date, columns=None, asset_type: str = "stock") -> pl.DataFrame:
        self.load_args = (symbols, start, end)
        self.load_asset_type = asset_type
        return self.panel

    def simulate_portfolio(self, panel, entries, exits, config, progress_cb=None, cancel_event=None) -> SimResult:
        self.sim_panel = panel
        self.sim_entries = entries
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
    assert engine.sim_panel is not None
    assert engine.sim_panel.height == 3
    assert engine.sim_panel.filter(pl.col("amount") == 0.0).height == 1
    assert engine.sim_entries is not None
    assert engine.sim_entries.to_list() == [True, False, True]
    assert engine.load_args is not None
    assert engine.load_args[1] < start  # warmup 只用于计算, 不参与正式交易


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
    engine.load_panel = lambda symbols, s, e, columns=None, asset_type="stock": panel  # type: ignore[method-assign]
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
