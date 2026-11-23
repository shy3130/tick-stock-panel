"""回归: close_t 建仓日的盘中 high 不得进入移动止损峰值。

close_t 按收盘价成交, 当日 high 发生在建仓之前; 把它计入峰值会把移动止损
线整体上移、退出提前 (方向偏保守但时间口径错误)。open_t+1 成交的当日 high
在建仓之后, 计入峰值是合法的 — 用对照组锁定不被误伤。

矩阵独立路径 / 矩阵组合路径 / legacy 组合路径三条路径同口径。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.backtest.engine import BacktestEngine, MatcherConfig

START = date(2024, 1, 1)


def _day(i: int, o: float, h: float, l: float, c: float) -> dict:
    return {
        "symbol": "A", "date": START + timedelta(days=i),
        "open": o, "high": h, "low": l, "close": c,
        "volume": 100_000,
        "signal_limit_up": False, "signal_limit_down": False,
    }


def _panel() -> pl.DataFrame:
    """day0 收盘 10.0 建仓 (close_t); 盘中曾冲高 12.0 (建仓前)。

    - 修复后峰值: day1 高点 10.2 → 峰值 10.2, 止损线 9.18;
      day2 low 9.0 ≤ 9.18 → day2 触发移动止损, 线价 9.18 成交。
    - 修复前峰值被 day0 high 12.0 污染: day1 线 10.8, day1 low 9.4 ≤ 10.8
      → day1 提前退出 (错误时间 + 错误价格)。
    """
    return pl.DataFrame([
        _day(0, 10.0, 12.0, 9.95, 10.0),   # 建仓日: 收盘 10.0, 盘中 12.0 在建仓前
        _day(1, 10.0, 10.2, 9.4, 9.5),
        _day(2, 9.5, 9.6, 9.0, 9.1),       # 线 9.18 被 low 9.0 穿越 → 止损
        _day(3, 9.1, 9.2, 8.9, 9.0),
        _day(4, 9.0, 9.1, 8.8, 8.9),
    ]).sort(["symbol", "date"])


def _masks(panel: pl.DataFrame, entry_days: set[int], exit_days: set[int]):
    dates = [r["date"] for r in panel.select(["symbol", "date"]).iter_rows(named=True)]
    entry = pl.Series([(dt - START).days in entry_days for dt in dates], dtype=pl.Boolean)
    exit_ = pl.Series([(dt - START).days in exit_days for dt in dates], dtype=pl.Boolean)
    return entry, exit_


def test_independent_close_t_entry_day_high_not_in_trailing_peak():
    panel = _panel()
    entry, exit_ = _masks(panel, {0}, set())
    cfg = MatcherConfig(matching="close_t", fees_pct=0, slippage_bps=0, trailing_stop_pct=0.10)
    result = BacktestEngine(repo=None).simulate_independent_candidates(  # type: ignore[arg-type]
        panel, entry, exit_, cfg,
    )
    assert result.stats.get("n_trades") == 1
    trade = result.trades[0]
    assert trade.exit_reason == "trailing_stop"
    assert trade.exit_date == "2024-01-03"        # day2, 而非 day1
    assert trade.exit_price == 10.2 * 0.9          # 峰值 10.2 的止损线 9.18


def test_portfolio_close_t_entry_day_high_not_in_trailing_peak():
    panel = _panel()
    entry, exit_ = _masks(panel, {0}, set())
    cfg = MatcherConfig(matching="close_t", fees_pct=0, slippage_bps=0, trailing_stop_pct=0.10)
    result = BacktestEngine(repo=None).simulate_portfolio(  # type: ignore[arg-type]
        panel, entry, exit_, cfg,
    )
    assert result.stats.get("n_trades") == 1
    trade = result.trades[0]
    assert trade.exit_reason == "trailing_stop"
    assert trade.exit_date == "2024-01-03"
    assert trade.exit_price == 10.2 * 0.9


def test_legacy_portfolio_close_t_entry_day_high_not_in_trailing_peak():
    panel = _panel()
    entry, exit_ = _masks(panel, {0}, set())
    cfg = MatcherConfig(matching="close_t", fees_pct=0, slippage_bps=0, trailing_stop_pct=0.10)
    result = BacktestEngine(repo=None).simulate_portfolio_legacy(  # type: ignore[arg-type]
        panel, entry, exit_, cfg,
    )
    assert result.stats.get("n_trades") == 1
    trade = result.trades[0]
    assert trade.exit_reason == "trailing_stop"
    assert trade.exit_date == "2024-01-03"
    assert trade.exit_price == 10.2 * 0.9


def test_open_fill_entry_day_high_still_counts():
    """对照组: open_t+1 次日开盘建仓, 当日 high 在建仓后 → 仍计入峰值。"""
    panel = _panel()
    entry, exit_ = _masks(panel, {0}, set())
    cfg = MatcherConfig(matching="open_t+1", fees_pct=0, slippage_bps=0, trailing_stop_pct=0.10)
    result = BacktestEngine(repo=None).simulate_independent_candidates(  # type: ignore[arg-type]
        panel, entry, exit_, cfg,
    )
    assert result.stats.get("n_trades") == 1
    trade = result.trades[0]
    # 建仓日 day1 开盘 10.0, 当日 high 10.2 计入峰值 → 线 9.18
    assert trade.exit_reason == "trailing_stop"
    assert trade.exit_date == "2024-01-03"
    assert trade.exit_price == 10.2 * 0.9


def _override_matrix(panel: pl.DataFrame, entry_day_index: int, override_price: float):
    """按分钟策略回测的真实构造: close_t + entry_price_override (触发分钟价)。"""
    import numpy as np

    from app.backtest.matrix import (
        build_market_data_matrix,
        build_market_matrix_from_signals,
        make_signal_matrix,
    )

    market = build_market_data_matrix(panel)
    shape = market.shape
    entry = np.zeros(shape, dtype=np.uint8)
    entry[entry_day_index, 0] = 1
    override = np.full(shape, np.nan, dtype=np.float32)
    override[entry_day_index, 0] = override_price
    signals = make_signal_matrix(shape, entry=entry, score=np.full(shape, 5.0))
    matrix = build_market_matrix_from_signals(
        market, signals,
        entry_delay_bars=0,
        exit_delay_bars=1,
        entry_price_override=override,
    )
    return matrix, int(entry.sum())


def test_minute_override_entry_day_high_counts_into_trailing_peak():
    """分钟策略回测 (close_t 硬编码 + 逐格分钟价覆盖): 建仓在触发分钟 (盘中),
    当日后续冲高应计入移动止损峰值 — 不得被 close_t 的收盘代理判断误排除。

    entry 09:35 @10.0, 当日 high 11.0 (建仓后) → 峰值 11.0, 止损线 9.9;
    次日 low 9.85 ≤ 9.9 → 次日触发, 线价 9.9 成交。误排除当日 high 会得到
    峰值 10.0 → 线 9.0 → 次日不触发 (退出更晚, 收益被高估)。
    """
    panel = pl.DataFrame([
        _day(0, 9.8, 11.0, 9.7, 10.5),   # 建仓日: 分钟价 10.0 盘中成交, 盘中冲高 11.0
        _day(1, 10.4, 10.6, 9.85, 9.9),  # 线 9.9 被 low 9.85 穿越 → 止损
        _day(2, 9.8, 9.9, 9.3, 9.4),
        _day(3, 9.4, 9.5, 9.1, 9.2),
    ]).sort(["symbol", "date"])
    matrix, raw_candidates = _override_matrix(panel, 0, 10.0)
    cfg = MatcherConfig(matching="close_t", fees_pct=0, slippage_bps=0, trailing_stop_pct=0.10)
    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]

    result = engine.simulate_market_matrix(matrix, cfg)
    assert result.stats.get("n_trades") == 1
    trade = result.trades[0]
    assert trade.exit_reason == "trailing_stop"
    assert trade.exit_date == "2024-01-02"
    assert trade.exit_price == 11.0 * 0.9

    # 独立矩阵路径同口径
    result2 = engine.simulate_independent_market_matrix(matrix, raw_candidates, cfg)
    assert result2.stats.get("n_trades") == 1
    trade2 = result2.trades[0]
    assert trade2.exit_reason == "trailing_stop"
    assert trade2.exit_date == "2024-01-02"
    assert trade2.exit_price == 11.0 * 0.9
