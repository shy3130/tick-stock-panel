from __future__ import annotations

from datetime import datetime

import polars as pl

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.matrix import build_market_matrix


def _three_market_matrix():
    moments = (
        datetime(2026, 7, 16, 10, 0),
        datetime(2026, 7, 16, 14, 0),
        datetime(2026, 7, 17, 10, 0),
    )
    lot_sizes = {"000001.SZ": 100, "1.HK": 500, "A.US": 1}
    rows = []
    entries = []
    exits = []
    for symbol, lot_size in lot_sizes.items():
        for time_id, moment in enumerate(moments):
            rows.append({
                "symbol": symbol,
                "name": symbol,
                "date": moment,
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "volume": 100_000,
                "score": 100.0,
                "lot_size": lot_size,
                "signal_limit_up": time_id == 0 and symbol != "000001.SZ",
                "signal_limit_down": time_id == 1 and symbol != "000001.SZ",
            })
            entries.append(time_id == 0)
            exits.append(time_id == 1)
    panel = pl.DataFrame(rows)
    return build_market_matrix(
        panel,
        pl.Series(entries, dtype=pl.Boolean),
        pl.Series(exits, dtype=pl.Boolean),
    )


def test_portfolio_matrix_applies_lots_sessions_limits_and_trade_identity() -> None:
    result = BacktestEngine(repo=None).simulate_market_matrix(
        _three_market_matrix(),
        MatcherConfig(
            matching="close_t",
            fees_pct=0,
            slippage_bps=0,
            max_positions=3,
            initial_capital=65_000,
        ),
    )

    trades = {trade.symbol: trade for trade in result.trades}
    assert set(trades) == {"000001.SZ", "1.HK", "A.US"}

    assert trades["000001.SZ"].shares % 100 == 0
    assert trades["1.HK"].shares % 500 == 0
    assert trades["A.US"].shares % 1 == 0
    assert trades["A.US"].shares % 100 != 0

    assert trades["000001.SZ"].exit_date == "2026-07-17"
    assert trades["1.HK"].exit_date == "2026-07-16"
    assert trades["A.US"].exit_date == "2026-07-16"
    assert result.stats["execution"]["sell_same_day_restricted"] == 1
    assert result.stats["execution"]["buy_limit_up"] == 0
    assert result.stats["execution"]["sell_limit_down"] == 0

    assert (trades["000001.SZ"].market, trades["000001.SZ"].currency) == ("cn", "CNY")
    assert (trades["1.HK"].market, trades["1.HK"].currency) == ("hk", "HKD")
    assert (trades["A.US"].market, trades["A.US"].currency) == ("us", "USD")


def test_independent_matrix_uses_one_market_lot_and_same_day_rules() -> None:
    result = BacktestEngine(repo=None).simulate_independent_market_matrix(
        _three_market_matrix(),
        raw_candidates=3,
        config=MatcherConfig(matching="close_t", fees_pct=0, slippage_bps=0),
    )

    trades = {trade.symbol: trade for trade in result.trades}
    assert trades["000001.SZ"].shares == 100
    assert trades["1.HK"].shares == 500
    assert trades["A.US"].shares == 1
    assert trades["000001.SZ"].exit_date == "2026-07-17"
    assert trades["1.HK"].exit_date == "2026-07-16"
    assert trades["A.US"].exit_date == "2026-07-16"
    assert result.stats["execution"]["sell_same_day_restricted"] == 1
