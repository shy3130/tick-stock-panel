"""Adjusted-curve accounting: terminal settlement and reported money reconcile."""

from datetime import date

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig, SimulationOptions
from app.backtest.matrix import (
    build_market_data_matrix,
    build_market_matrix_from_signals,
    make_signal_matrix,
)


def _run(*, blocked=False, suspended=False, legacy=False, independent=False, curves=True, commission=0.001):
    prices = [10.0, 10.0, 9.0]
    frame = pl.DataFrame({
        "symbol": ["A"] * 3,
        "date": [date(2025, 1, n) for n in (2, 3, 6)],
        **{name: prices for name in ("open", "high", "low", "close")},
        "volume": [1000.0, 1000.0, 0.0 if suspended else 1000.0],
        "signal_limit_down": [False, False, blocked],
    })
    entries = pl.Series([True, False, False])
    exits = pl.Series([False, False, False])
    config = MatcherConfig(
        initial_capital=1100, max_positions=1, commission_pct=commission,
        stamp_tax_pct=0, slippage_bps=0,
    )
    engine = BacktestEngine(None)
    if independent:
        method = engine.simulate_independent_candidates_legacy if legacy else engine.simulate_independent_candidates
        return method(frame, entries, exits, config)
    if legacy:
        return engine.simulate_portfolio_legacy(frame, entries, exits, config)
    market = build_market_data_matrix(frame)
    signals = make_signal_matrix(
        market.shape, entry=entries.to_numpy().astype(np.uint8)[:, None],
    )
    return engine.simulate_market_matrix(
        build_market_matrix_from_signals(market, signals), config,
        options=SimulationOptions(include_curves=curves, include_monte_carlo=False),
    )


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("kind", ["limit_down", "suspension"])
def test_terminal_settlement_reconciles_curve_cash_and_trades(legacy, kind):
    result = _run(blocked=kind == "limit_down", suspended=kind == "suspension", legacy=legacy)
    trade, = result.trades
    expected = round(1100 + sum(t.pnl_amount for t in result.trades), 2)
    assert expected == 998.10
    assert result.stats["final_equity"] == expected
    assert result.equity_curve[-1]["value"] == expected
    assert result.equity_curve[-1]["cash"] == expected
    assert result.equity_curve[-1]["positions"] == 0
    assert result.equity_curve[-1]["exposure"] == 0
    assert result.stats["pending_exit_positions"] == 1
    assert result.stats["terminal_settlement_count"] == 1
    assert trade.blocked_exit_days == 1
    assert result.drawdown_curve[-1]["value"] == round(expected / 1100 - 1, 4)
    assert result.stats["max_drawdown"] == result.drawdown_curve[-1]["value"]


def test_terminal_settlement_reconciles_when_curves_are_omitted():
    result = _run(blocked=True, curves=False)
    assert result.equity_curve == []
    assert result.drawdown_curve == []
    assert result.stats["final_equity"] == round(1100 + result.trades[0].pnl_amount, 2)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("independent", [False, True])
def test_cash_precision_matches_displayed_trade_amounts(legacy, independent):
    result = _run(legacy=legacy, independent=independent, commission=0.000272)
    trade, = result.trades
    assert trade.pnl_amount == round(trade.exit_value - trade.entry_value, 2)
    if not independent:
        assert result.stats["final_equity"] == round(1100 + trade.pnl_amount, 2)


def test_normal_terminal_exit_keeps_existing_curve_result():
    result = _run()
    assert result.stats["final_equity"] == 998.10
    assert result.stats["pending_exit_positions"] == 0
    assert result.stats["terminal_settlement_count"] == 0


def test_drawdown_peak_includes_initial_capital_before_first_day_fee():
    stats = BacktestEngine._calc_portfolio_stats_from_values(
        [999.0, 999.0], [1.0, 0.0], [], 1000.0, include_monte_carlo=False,
    )
    assert stats["max_drawdown"] == -0.001
