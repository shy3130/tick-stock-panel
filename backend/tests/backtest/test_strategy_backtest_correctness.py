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

    def load_panel(self, symbols, start: date, end: date) -> pl.DataFrame:
        self.load_args = (symbols, start, end)
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

    class _CandidateRepo:
        def __init__(self) -> None:
            self.benchmark_reads = 0

        def get_index_daily(self, *args, **kwargs) -> pl.DataFrame:
            self.benchmark_reads += 1
            return pl.DataFrame(
                {
                    "date": [start, start + timedelta(days=1)],
                    "close": [100.0, 110.0],
                }
            )

    repo = _CandidateRepo()
    engine = BacktestEngine(repo=repo)  # type: ignore[arg-type]
    engine.load_panel = lambda symbols, s, e: panel  # type: ignore[method-assign]
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
    # 候选曲线只含退出事件日，不是连续日频收益：严禁按 252 年化或伪造日频风险指标。
    assert result.stats["annual_return"] is None
    assert result.stats["sharpe"] is None
    assert "annual_volatility" not in result.stats
    assert "sortino" not in result.stats
    assert "metric_context" not in result.stats
    assert result.benchmark_curve == []
    assert repo.benchmark_reads == 0
    assert "benchmark_return" not in result.stats
    assert "alpha" not in result.stats
    assert result.stats["time_series_metrics_available"] is False
    assert result.stats["curve_semantics"] == "candidate_exit_event_average"


def test_portfolio_stats_include_advanced_risk_and_cost_breakdown():
    stats = BacktestEngine._calc_portfolio_stats(
        [
            {"date": "2024-01-01", "value": 1_000_000.0, "exposure": 0.0},
            {"date": "2024-01-02", "value": 1_010_000.0, "exposure": 0.5},
            {"date": "2024-01-03", "value": 1_005_000.0, "exposure": 0.25},
        ],
        [],
        1_000_000.0,
        fees_pct=0.0002,
        slippage_bps=5.0,
    )

    assert "sortino" in stats
    assert "value_at_risk" in stats
    assert stats["cost_breakdown"]["total"] == 0.0
    assert stats["cost_breakdown"]["turnover"] == 0.0


def test_position_mode_benchmark_window_matches_actual_equity_coverage():
    """position 模式请求区间宽于实际 panel 覆盖时, benchmark 只按实际净值区间计算。"""
    start = date(2024, 1, 1)
    rows = []
    for i in range(4):
        rows.append({
            "symbol": "A",
            "name": "A",
            "date": date(2024, 1, 2) + timedelta(days=i),
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 100_000,
            "amount": 1000.0,
            "signal_limit_up": False,
            "signal_limit_down": False,
        })
    panel = pl.DataFrame(rows).sort(["symbol", "date"])

    class _WindowRepo:
        def __init__(self) -> None:
            self.requested: tuple[date, date] | None = None

        def get_index_daily(self, symbol, start, end, columns) -> pl.DataFrame:
            self.requested = (start, end)
            days = []
            d = start
            while d <= end:
                days.append({"date": d, "close": 100.0 + 2.0 * d.day})
                d += timedelta(days=1)
            return pl.DataFrame(days)

    class _NarrowEngine:
        def __init__(self) -> None:
            self.repo = _WindowRepo()

        def load_panel(self, symbols, start, end) -> pl.DataFrame:
            return panel

        def simulate_portfolio(self, panel, entries, exits, config, progress_cb=None, cancel_event=None) -> SimResult:
            dates = panel["date"].to_list()
            last = len(dates) - 1
            return SimResult(
                equity_curve=[
                    {
                        "date": str(d)[:10],
                        "value": config.initial_capital * 1.1 if i == last else config.initial_capital,
                    }
                    for i, d in enumerate(dates)
                ],
                drawdown_curve=[],
                trades=[],
                per_symbol_stats=[],
                stats={"total_return": 0.1, "n_trades": 1},
            )

    engine = _NarrowEngine()
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub(_strategy()))

    result = service.run(StrategyBacktestConfig(
        strategy_id="test",
        symbols=None,
        start=start,
        end=date(2024, 1, 31),
        matching="close_t",
        mode="position",
    ))

    assert result.error is None
    # benchmark 窗口 = 组合净值实际覆盖 [01-02, 01-05], 而非请求区间 [01-01, 01-31]
    assert engine.repo.requested == (date(2024, 1, 2), date(2024, 1, 5))
    # index close: 01-02=104, 01-05=110 → 只按实际区间计算收益
    assert result.stats["benchmark_return"] == round(110.0 / 104.0 - 1, 4)
    assert result.stats["excess"] == round(0.1 - (110.0 / 104.0 - 1), 4)


def test_benchmark_selection_and_execution_config_are_preserved():
    class _BenchmarkRepo:
        requested_symbol: str | None = None

        def get_index_daily(self, symbol, start, end, columns):
            self.requested_symbol = symbol
            return pl.DataFrame({
                "date": [start, end],
                "close": [100.0, 105.0],
            })

    repo = _BenchmarkRepo()
    service = StrategyBacktestService(
        engine=SimpleNamespace(repo=repo),
        strategy_engine=_StrategyEngineStub(_strategy()),
    )
    config = StrategyBacktestConfig(
        strategy_id="test",
        symbols=None,
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        matching="open_t+1",
        entry_fill="open_t+1",
        exit_fill="close_t",
        benchmark_symbol="000300.INDEX",
    )

    curve = service._build_benchmark_curve(
        config.start,
        config.end,
        config.benchmark_symbol,
    )
    encoded = service._config_to_dict(config)

    assert repo.requested_symbol == "000300.INDEX"
    assert curve[0]["name"] == "沪深300"
    assert encoded["benchmark_symbol"] == "000300.INDEX"
    assert encoded["entry_fill"] == "open_t+1"
    assert encoded["exit_fill"] == "close_t"
