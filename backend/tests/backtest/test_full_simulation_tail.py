"""全量模拟 (full mode) 尾部执行回归测试。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.backtest.engine import BacktestEngine, MatcherConfig


def _panel_with_tail(symbols: list[str], n_data_days: int) -> pl.DataFrame:
    start = date(2024, 1, 1)
    rows = []
    for sym in symbols:
        for i in range(n_data_days):
            px = 10.0 + i
            rows.append({
                "symbol": sym,
                "date": start + timedelta(days=i),
                "open": px,
                "high": px,
                "low": px,
                "close": px,
                "volume": 100_000,
                "signal_limit_up": False,
                "signal_limit_down": False,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def test_full_simulation_executes_signal_at_tail():
    """信号集中在正式区间最后一天时, tail 数据应允许次日开盘买入并按策略退出。"""
    n_days = 6
    panel = _panel_with_tail(["A"], n_days + 3)

    start = date(2024, 1, 1)
    end = start + timedelta(days=n_days - 1)
    entry_vals = []
    for row in panel.select(["symbol", "date"]).iter_rows(named=True):
        entry_vals.append(row["date"] == end)
    entry_mask = pl.Series(entry_vals, dtype=pl.Boolean)
    exit_mask = pl.Series([False] * len(panel), dtype=pl.Boolean)

    result = BacktestEngine(repo=None).simulate_independent_candidates(  # type: ignore[arg-type]
        panel,
        entry_mask,
        exit_mask,
        MatcherConfig(matching="open_t+1", fees_pct=0, slippage_bps=0, max_hold_days=2),
    )

    assert not result.stats.get("error"), f"unexpected error: {result.stats.get('error')}"
    assert result.stats.get("full_kind") == "candidate_execution"
    assert result.stats.get("n_candidates") == 1
    assert result.stats.get("n_trades") == 1
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_signal_date == str(end)
    assert trade.entry_date == str(end + timedelta(days=1))
    assert trade.exit_reason == "max_hold"
