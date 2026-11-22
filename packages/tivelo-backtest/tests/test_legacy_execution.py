"""Independent numerical examples; no workbench, repository or data engine."""

from dataclasses import asdict
from datetime import date
from threading import Event
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from tivelo_backtest.legacy.daily_optimization import optimize_daily_matrix
from tivelo_backtest.legacy.factor_rank import FactorRankResearchMatrixStrategy
from tivelo_backtest.legacy.panel_strategy import LegacyPolarsStrategy
from tivelo_backtest.legacy.simulator import LegacySimulator, MatcherConfig, SimulationOptions
from tivelo_backtest.research.matrix import (
    build_market_data_matrix,
    build_market_matrix,
    build_market_matrix_from_signals,
    make_signal_matrix,
)
from tivelo_backtest.research.scoring import apply_matrix_selection

DAYS = [date(2025, 1, day) for day in (2, 3, 6, 7, 8, 9)]


def panel(symbols=("600001.SH",)):
    return pl.DataFrame(
        [
            dict(
                symbol=symbol,
                date=day,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=100_000.0,
                score=1.0,
                signal_limit_up=False,
                signal_limit_down=False,
            )
            for symbol in symbols
            for day, price in zip(DAYS, (10.0, 10.0, 10.0, 11.0, 11.0, 11.0), strict=True)
        ]
    )


def signals(symbol_count=1):
    return (
        pl.Series([True, False, False, False, False, False] * symbol_count),
        pl.Series([False, False, True, False, False, False] * symbol_count),
    )


def config(**overrides):
    values = dict(
        matching="open_t+1",
        commission_pct=0,
        stamp_tax_pct=0,
        slippage_bps=0,
        initial_capital=10_000,
        max_positions=1,
    )
    values.update(overrides)
    return MatcherConfig(**values)


def matrix(frame=None, *, delay=1):
    frame = panel() if frame is None else frame
    entry, exit_ = frame["date"] == DAYS[0], frame["date"] == DAYS[2]
    return build_market_matrix(frame, entry, exit_, entry_delay_bars=delay, exit_delay_bars=delay)


def run(frame=None, *, simulator=None, delay=1, **overrides):
    return (simulator or LegacySimulator()).simulate_market_matrix(
        matrix(frame, delay=delay),
        config(**overrides),
        options=SimulationOptions(include_monte_carlo=False),
    )


@pytest.mark.parametrize(
    "commission,stamp,shares,profit",
    [
        (0, 0, 1000, 1000),
        (0.001, 0.002, 900, 861.30),
    ],
)
def test_cash_quantity_costs_and_signal_dates(commission, stamp, shares, profit):
    result = run(commission_pct=commission, stamp_tax_pct=stamp)
    (trade,) = result.trades
    assert (trade.entry_price, trade.exit_price) == (10, 11)
    assert (str(trade.entry_date), str(trade.exit_date)) == ("2025-01-03", "2025-01-07")
    assert str(trade.entry_signal_date) == "2025-01-02"
    assert str(trade.exit_signal_date) == "2025-01-06"
    assert trade.shares == shares
    assert trade.pnl_amount == pytest.approx(profit)
    assert result.equity_curve[-1]["value"] == pytest.approx(10_000 + profit)


@pytest.mark.parametrize("path", ["simulate_portfolio", "simulate_portfolio_legacy"])
def test_long_panel_entry_points_keep_same_trade_and_cash_result(path):
    entries, exits = signals()
    result = getattr(LegacySimulator(), path)(panel(), entries, exits, config())
    assert [asdict(trade) for trade in result.trades] == [asdict(trade) for trade in run().trades]
    assert result.equity_curve[-1]["value"] == 11_000


@pytest.mark.parametrize(
    "path", ["simulate_independent_candidates", "simulate_independent_candidates_legacy"]
)
def test_independent_candidate_paths_use_same_delayed_prices(path):
    entries, exits = signals()
    (trade,) = getattr(LegacySimulator(), path)(panel(), entries, exits, config()).trades
    assert (trade.entry_price, trade.exit_price, trade.duration) == (10, 11, 2)
    assert trade.pnl_pct == pytest.approx(0.1)


@pytest.mark.parametrize("column,value", [("signal_limit_up", True), ("volume", 0.0)])
def test_nontradable_entry_never_spends_cash(column, value):
    frame = panel().with_columns(
        pl.when(pl.col("date") == DAYS[1])
        .then(pl.lit(value))
        .otherwise(pl.col(column))
        .alias(column)
    )
    result = run(frame)
    assert result.trades == []
    assert result.equity_curve[-1]["value"] == 10_000


def test_limit_down_exit_is_deferred_and_keeps_original_signal():
    frame = panel().with_columns((pl.col("date") == DAYS[3]).alias("signal_limit_down"))
    result = run(frame)
    (trade,) = result.trades
    assert str(trade.exit_date) == "2025-01-08"
    assert str(trade.exit_signal_date) == "2025-01-06"
    assert trade.blocked_exit_days == 1
    assert result.equity_curve[-1]["value"] == 11_000


def test_equal_scores_with_cash_for_one_position_have_stable_symbol_order():
    frame = panel(("600002.SH", "600001.SH"))
    assert [trade.symbol for trade in run(frame).trades] == ["600001.SH"]
    assert [trade.symbol for trade in run(frame.reverse()).trades] == ["600001.SH"]


def test_polars_filter_keeps_panel_precision_and_scores_before_entry_triggers():
    frame = pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH", "600003.SH"],
        "date": [DAYS[0]] * 3,
        "close": [10.00000001, 10.00000004, 11.0],
        "ext_quality": [1.0, 2.0, 3.0],
        "signal_buy": [True, True, False],
        "signal_sell": [True, False, False],
    })
    strategy = LegacyPolarsStrategy(SimpleNamespace(
        filter=lambda panel, params: pl.col("close") >= params["minimum"],
        META={"params": {"minimum": 10.00000002}, "scoring": {"ext_quality": 1.0}},
        BASIC_FILTER={"enabled": False},
        ENTRY_SIGNALS=["buy"],
        EXIT_SIGNALS=["signal_sell"],
    ), "{}")
    scored, candidate, entry, exits = strategy.evaluate(frame, {})
    assert candidate.to_list() == [False, True, True]
    assert scored["score"].to_list() == [0, 0, 100]
    assert entry.to_list() == [False, True, False]
    assert exits.to_list() == [True, False, False]


def test_prepared_legacy_factor_rank_signals_drive_next_open_fills():
    symbols = ("600001.SH", "600002.SH", "600003.SH")
    frame = panel(symbols).with_columns(
        pl.Series("amount", [3, 2, 1, 2, 2, 2, 3, 2, 3, 2, 2, 2, 1, 2, 3, 2, 2, 2]),
        pl.when(pl.col("symbol") == symbols[0])
        .then(pl.col("volume"))
        .otherwise(0.0)
        .alias("volume"),
    )
    market = build_market_data_matrix(frame, field_columns={"amount"})
    strategy = FactorRankResearchMatrixStrategy(scoring={"amount": 1.0})
    signals = strategy.compute_signals(market, {"top_rank": 1})
    assert signals.score[:3].tolist() == [[100, 100, 0], [50, 50, 50], [0, 100, 100]]
    assert signals.entry[:3].tolist() == [[1, 0, 0], [0, 0, 0], [0, 1, 0]]
    candidates = np.ones(market.shape, dtype=bool)
    candidates[2, 1] = False
    selected = apply_matrix_selection(market, signals, candidates)
    assert selected.score[:3].tolist() == [[100, 0, 0], [0, 0, 0], [0, 0, 0]]
    np.testing.assert_array_equal(selected.exit, signals.exit)
    matched = build_market_matrix_from_signals(
        market, selected, entry_delay_bars=1, exit_delay_bars=1
    )
    result = LegacySimulator().simulate_market_matrix(
        matched, config(), options=SimulationOptions(include_monte_carlo=False)
    )
    (trade,) = result.trades
    assert trade.symbol == symbols[0]
    assert (str(trade.entry_date), str(trade.exit_date)) == ("2025-01-03", "2025-01-07")
    assert (trade.entry_signal_date, trade.exit_signal_date) == ("2025-01-02", "2025-01-06")
    assert trade.shares == 1_000
    assert result.equity_curve[-1]["value"] == 11_000


def test_standalone_robust_optimization_bounds_warmup_and_ranks_real_fills():
    dates = [date(2024, 12, 31), *(date(2025, 1, d) for d in (2, 3, 14, 16, 17, 28))]
    prices = [9.0, 10.0, 10.0, 12.0, 13.0, 13.0, 16.0]
    market = build_market_data_matrix(
        pl.DataFrame(
            {
                "symbol": ["600001.SH"] * len(dates),
                "date": dates,
                "volume": [100_000.0] * len(dates),
                **{name: prices for name in ("open", "high", "low", "close")},
            }
        )
    )
    seen = []

    class PriceThreshold:
        def required_warmup_bars(self, params):
            return 1

        def compute_signals(self, view, params):
            seen.append((view.timestamp_labels[0][:10], view.timestamp_labels[-1][:10]))
            entry = view.close >= params["minimum"]
            entry[0] = False
            return make_signal_matrix(
                view.shape, entry=entry, exit=np.zeros(view.shape, dtype=bool)
            )

    result = optimize_daily_matrix(
        market,
        PriceThreshold(),
        config(),
        start=date(2025, 1, 1),
        end=date(2025, 1, 28),
        params_meta=[{"id": "minimum", "type": "int"}],
        param_grid={"minimum": [13, 10]},
        objective="total_return",
        k_segments=2,
        min_trades_total=2,
        stage2_top_n=1,
    )
    assert seen == [
        ("2024-12-31", "2025-01-14"),
        ("2024-12-31", "2025-01-14"),
        ("2025-01-14", "2025-01-28"),
        ("2024-12-31", "2025-01-28"),
    ]
    winner, failed = result["results"]
    assert result["best_params"] == {"minimum": 10}
    assert winner["oos_values"] == [0.2, 0.21] and winner["trades_total"] == 2
    assert winner["score"] == pytest.approx(0.203)
    assert winner["is_full"]["objective_raw"] == 0.6
    assert failed["params"] == {"minimum": 13} and "未产生买入信号" in failed["error"]


def test_position_policy_context_and_legacy_fallback_are_preserved():
    seen = []

    def broken(ctx):
        seen.append(ctx)
        raise ValueError("synthetic policy failure")

    result = run(position_sizing="policy", position_policy_compute=broken)
    assert len(seen) == 1
    assert seen[0].closed_trades == ()
    assert seen[0].portfolio.cash == 10_000
    assert result.trades[0].shares == 1000
    assert result.stats["execution"]["position_policy_fallback"] == 1


def test_minutes_are_injected_once_with_explicit_asset_type():
    calls = []

    def minutes(symbols, dates, asset_type):
        calls.append((set(symbols), dates, asset_type))
        # Legacy minute payload: volume is in 100-share lots, amount is currency.
        return {
            (symbol, day): np.array([[price, price, price, price, 100, price * 10_000]])
            for symbol in symbols
            for day in dates
            for price in [10.5 if day == "2025-01-03" else 11.5]
        }

    (trade,) = run(
        simulator=LegacySimulator(minute_loader=minutes), minute_fill=True, asset_type="etf"
    ).trades
    assert calls == [({"600001.SH"}, {"2025-01-03", "2025-01-07"}, "etf")]
    assert (trade.entry_price, trade.exit_price) == (10.5, 11.5)


def test_cancelled_legacy_run_produces_no_trades():
    cancellation = Event()
    cancellation.set()
    result = LegacySimulator().simulate_market_matrix(matrix(), config(), cancel_event=cancellation)
    assert result.trades == []


def test_shared_arrays_can_be_reused_across_trials_without_mutation():
    prepared = matrix()
    before = {
        name: value.tobytes()
        for name, value in vars(prepared).items()
        if isinstance(value, np.ndarray)
    }
    for capital in (10_000, 20_000):
        result = LegacySimulator().simulate_market_matrix(
            prepared,
            config(initial_capital=capital),
            options=SimulationOptions(include_monte_carlo=False),
        )
        assert result.equity_curve[-1]["value"] == capital * 1.1
    assert before == {
        name: value.tobytes()
        for name, value in vars(prepared).items()
        if isinstance(value, np.ndarray)
    }
    with pytest.raises(ValueError, match="read-only"):
        prepared.close[0, 0] = 999
