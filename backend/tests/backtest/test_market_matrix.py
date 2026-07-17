from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig, SimulationOptions
from app.backtest.matrix import build_market_matrix


def _row(symbol: str, day: int, price: float, **overrides) -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "date": date(2024, 1, 1) + timedelta(days=day),
        "open": overrides.get("open", price),
        "high": overrides.get("high", price),
        "low": overrides.get("low", price),
        "close": overrides.get("close", price),
        "volume": overrides.get("volume", 100_000),
        "score": overrides.get("score", 0.0),
        "signal_limit_up": overrides.get("signal_limit_up", False),
        "signal_limit_down": overrides.get("signal_limit_down", False),
        "signal_entry": overrides.get("signal_entry", False),
        "signal_exit": overrides.get("signal_exit", False),
    }


def test_sparse_mapping_is_stable_read_only_and_not_forward_filled():
    panel = pl.DataFrame([
        _row("B", 1, 20, signal_entry=True),
        _row("A", 2, 12),
        _row("A", 0, 10, signal_entry=True),
    ])
    entries = panel["signal_entry"]
    matrix = build_market_matrix(panel, entries, None)

    assert matrix.symbols == ("A", "B")
    assert matrix.timestamp_labels == ("2024-01-01", "2024-01-02", "2024-01-03")
    assert matrix.shape == (3, 2)
    assert np.isnan(matrix.close[1, 0])
    assert matrix.tradable[1, 0] == 0
    assert matrix.entry[1, 0] == 0
    assert matrix.close.flags.writeable is False
    assert matrix.entry.flags.writeable is False

    reversed_panel = panel.reverse()
    reversed_matrix = build_market_matrix(reversed_panel, reversed_panel["signal_entry"], None)
    np.testing.assert_allclose(matrix.close, reversed_matrix.close, equal_nan=True)
    np.testing.assert_array_equal(matrix.entry, reversed_matrix.entry)


def test_duplicate_timestamp_symbol_is_rejected():
    panel = pl.DataFrame([_row("A", 0, 10), _row("A", 0, 11)])
    with pytest.raises(ValueError, match="unique timestamp/symbol"):
        build_market_matrix(panel, None, None)


def test_intraday_timestamps_share_daily_session_id():
    panel = pl.DataFrame({
        "symbol": ["A", "A", "A"],
        "datetime": [
            "2024-01-01 09:30:00",
            "2024-01-01 10:30:00",
            "2024-01-02 09:30:00",
        ],
        "open": [10.0, 10.1, 10.2],
        "high": [10.0, 10.1, 10.2],
        "low": [10.0, 10.1, 10.2],
        "close": [10.0, 10.1, 10.2],
        "volume": [100, 100, 100],
    }).with_columns(pl.col("datetime").str.to_datetime())

    matrix = build_market_matrix(panel, None, None)
    assert matrix.session_ids.tolist() == [0, 0, 1]


def test_tradable_matches_legacy_suspension_rules():
    panel = pl.DataFrame([
        _row("A", 0, 10, volume=100),
        _row("B", 0, 10, volume=0),
        _row("C", 0, 10, volume=0, high=11, low=9),
        _row("D", 0, 0, volume=100),
    ])
    matrix = build_market_matrix(panel, None, None)
    tradable = dict(zip(matrix.symbols, matrix.tradable[0].tolist()))
    assert tradable == {"A": 1, "B": 0, "C": 1, "D": 0}


def test_open_t_plus_one_keeps_legacy_next_asset_bar_semantics():
    panel = pl.DataFrame([
        _row("A", 0, 10, signal_entry=True),
        _row("B", 1, 20),
        _row("A", 2, 12),
    ])
    matrix = build_market_matrix(
        panel,
        panel["signal_entry"],
        None,
        entry_delay_bars=1,
        entry_signal_ids=["signal_entry"],
    )

    assert matrix.entry[:, 0].tolist() == [0, 0, 1]
    assert matrix.entry_signal_time[2, 0] == 0


def test_matrix_matcher_matches_legacy_trade_records_and_equity():
    rows = []
    for symbol, score in (("A", 90), ("B", 80), ("C", 70)):
        for day in range(5):
            overrides = {"score": score}
            if symbol == "A" and day == 2:
                overrides.update(open=9, high=9, low=9, close=9, signal_limit_down=True)
            if symbol == "B" and day == 3:
                overrides.update(open=8.5, high=9, low=8, close=8.5)
            rows.append(_row(symbol, day, 10 + day * 0.1, **overrides))
    panel = pl.DataFrame(rows).sort(["symbol", "date"])
    entries = pl.Series([
        row["date"] == date(2024, 1, 1)
        for row in panel.select("date").iter_rows(named=True)
    ])
    exits = pl.Series([
        row["symbol"] == "A" and row["date"] == date(2024, 1, 2)
        for row in panel.select(["symbol", "date"]).iter_rows(named=True)
    ])
    config = MatcherConfig(
        matching="open_t+1",
        fees_pct=0,
        slippage_bps=0,
        max_positions=2,
        max_exposure_pct=0.8,
        stop_loss_pct=0.1,
        initial_capital=100_000,
    )
    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]

    matrix_result = engine.simulate_portfolio(panel, entries, exits, config)
    legacy_result = engine.simulate_portfolio_legacy(panel, entries, exits, config)

    assert [asdict(trade) for trade in matrix_result.trades] == [
        asdict(trade) for trade in legacy_result.trades
    ]
    assert matrix_result.equity_curve == legacy_result.equity_curve
    assert matrix_result.drawdown_curve == legacy_result.drawdown_curve
    assert matrix_result.stats["execution"] == legacy_result.stats["execution"]
    assert matrix_result.stats["pending_exit_positions"] == legacy_result.stats["pending_exit_positions"]


def test_independent_matrix_matches_legacy_candidates():
    panel = pl.DataFrame([
        _row("A", 0, 10, signal_entry=True),
        _row("A", 1, 11, signal_entry=True),
        _row("A", 2, 12),
        _row("A", 3, 9, low=8.5),
        _row("A", 4, 10),
    ]).sort(["symbol", "date"])
    entries = panel["signal_entry"]
    exits = panel["signal_exit"]
    config = MatcherConfig(
        matching="close_t",
        fees_pct=0,
        slippage_bps=0,
        max_hold_days=2,
        stop_loss_pct=0.1,
    )
    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]

    matrix_result = engine.simulate_independent_candidates(panel, entries, exits, config)
    legacy_result = engine.simulate_independent_candidates_legacy(panel, entries, exits, config)

    assert [asdict(trade) for trade in matrix_result.trades] == [
        asdict(trade) for trade in legacy_result.trades
    ]
    assert matrix_result.stats["execution"] == legacy_result.stats["execution"]


def test_lightweight_portfolio_keeps_stats_without_curves_or_monte_carlo(monkeypatch):
    panel = pl.DataFrame([
        _row("A", 0, 10, signal_entry=True),
        _row("A", 1, 11),
        _row("A", 2, 12, signal_exit=True),
        _row("A", 3, 11),
    ]).sort(["symbol", "date"])
    matrix = build_market_matrix(
        panel,
        panel["signal_entry"],
        panel["signal_exit"],
    )
    config = MatcherConfig(
        matching="close_t",
        fees_pct=0,
        slippage_bps=0,
        max_positions=1,
        initial_capital=100_000,
    )
    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]
    full = engine.simulate_market_matrix(matrix, config)

    def unexpected(_pnls):
        raise AssertionError("Monte Carlo should not run")

    monkeypatch.setattr(BacktestEngine, "_mc_drawdown_percentiles", unexpected)
    light = engine.simulate_market_matrix(
        matrix,
        config,
        options=SimulationOptions(
            include_monte_carlo=False,
            include_curves=False,
            include_trades=False,
            include_per_symbol_stats=False,
            include_return_distribution=False,
        ),
    )

    assert light.equity_curve == []
    assert light.drawdown_curve == []
    assert light.trades == []
    assert light.per_symbol_stats == []
    assert "mc_maxdd_p50" not in light.stats
    for name in ("total_return", "annual_return", "max_drawdown", "sharpe", "sortino"):
        assert light.stats[name] == full.stats[name]
