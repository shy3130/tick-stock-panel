"""回归: entry_fill=close_t 时收盘封涨停(非一字)的信号日不得按收盘价成交。

用户 bug 报告①: _can_buy 只拦「一字板」(四价合一), 尾盘封板(盘中正常交易、
收盘=涨停价)的股票在 close_t 口径下以涨停收盘价"成交" — 现实中收盘封板的
买队排队排不进, 回测收益系统性虚高(报告实测 55.7% 买单落在涨停收盘日)。

修复: close_t 口径下凡收盘涨停(limit_up_locked, 含一字)一律拦截;
open_t+1 口径按次日开盘成交, 保持只拦次日一字板的既有行为。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.backtest.engine import BacktestEngine, MatcherConfig


def _panel(limit_day_idx: int | None, one_price: bool) -> pl.DataFrame:
    """单标的 10 日面板; limit_day_idx 当日收盘涨停(one_price=True 为一字)。"""
    start = date(2024, 1, 1)
    rows = []
    for i in range(10):
        px = 10.0 + i * 0.05
        if i == limit_day_idx:
            prev_close = 10.0 + (i - 1) * 0.05
            limit = round(prev_close * 1.1, 2)
            if one_price:
                o = h = l = c = limit
            else:
                o, l = px, px - 0.1
                h = c = limit
            rows.append({
                "symbol": "A", "date": start + timedelta(days=i),
                "open": o, "high": h, "low": l, "close": c,
                "volume": 100_000,
                "signal_limit_up": True, "signal_limit_down": False,
            })
        else:
            rows.append({
                "symbol": "A", "date": start + timedelta(days=i),
                "open": px, "high": px + 0.02, "low": px - 0.02, "close": px,
                "volume": 100_000,
                "signal_limit_up": False, "signal_limit_down": False,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _run(panel: pl.DataFrame, signal_day: date, matching: str):
    entry_mask = pl.Series(
        [r["date"] == signal_day for r in panel.select(["symbol", "date"]).iter_rows(named=True)],
        dtype=pl.Boolean,
    )
    exit_mask = pl.Series([False] * len(panel), dtype=pl.Boolean)
    return BacktestEngine(repo=None).simulate_independent_candidates(  # type: ignore[arg-type]
        panel, entry_mask, exit_mask,
        MatcherConfig(matching=matching, fees_pct=0, slippage_bps=0, max_hold_days=1),
    )


def test_close_t_blocks_sealed_limit_up_close():
    """非一字、收盘封涨停 → close_t 不得成交(修复点)。"""
    result = _run(_panel(limit_day_idx=5, one_price=False), date(2024, 1, 6), "close_t")
    assert result.stats.get("n_trades") == 0
    assert result.trades == []


def test_close_t_fills_normal_close():
    """正常信号日(无涨停) → close_t 照常以当日收盘价成交。"""
    result = _run(_panel(limit_day_idx=None, one_price=False), date(2024, 1, 6), "close_t")
    assert result.stats.get("n_trades") == 1
    trade = result.trades[0]
    assert trade.entry_date == "2024-01-06"
    assert trade.entry_price == 10.25  # 当日收盘


def test_open_t1_allows_entry_after_limit_up_close_signal():
    """信号日收盘封板不阻碍次日开盘买入(open_t+1 不受新拦截影响)。"""
    result = _run(_panel(limit_day_idx=5, one_price=False), date(2024, 1, 6), "open_t+1")
    assert result.stats.get("n_trades") == 1
    trade = result.trades[0]
    assert trade.entry_date == "2024-01-07"
    assert trade.entry_price == 10.30  # 次日开盘


def test_open_t1_still_blocks_one_price_limit_fill_day():
    """次日一字涨停 → open_t+1 既有拦截保持不变。"""
    result = _run(_panel(limit_day_idx=6, one_price=True), date(2024, 1, 6), "open_t+1")
    assert result.stats.get("n_trades") == 0
    assert result.trades == []
