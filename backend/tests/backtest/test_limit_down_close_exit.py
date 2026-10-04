"""回归: exit_fill=close_t 时收盘封死跌停(非一字)不得按收盘价卖出。

涨停拒买的镜像问题: _can_sell 只拦「一字跌停」, 尾盘封板股按跌停收盘价
"卖出" — 现实中收盘封死的卖队排不出去, 亏损被系统性低估。修复后:
- close_t 计划出场(信号/到期/期末)遇收盘封跌停 → 拦截并经 pending_exit
  递延到下一可卖日;
- 日内风控线(止损/移动止损/止盈, override 盘中价)不受收盘封板影响;
- 期末仍被递延的持仓不再静默丢弃, 按末日价格强制记录平仓
  (blocked_exit_days>0 保留"曾受阻"痕迹);
- open_t+1 按次日开盘卖出, 保持既有行为。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.backtest.engine import BacktestEngine, MatcherConfig

START = date(2024, 1, 1)


def _day(i: int, o: float, h: float, l: float, c: float, *, ld: bool = False) -> dict:
    return {
        "symbol": "A", "date": START + timedelta(days=i),
        "open": o, "high": h, "low": l, "close": c,
        "volume": 100_000,
        "signal_limit_up": False, "signal_limit_down": ld,
    }


def _normal(i: int) -> dict:
    px = 10.0 + i * 0.05
    return _day(i, px, px + 0.02, px - 0.02, px)


def _panel(days: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(days).sort(["symbol", "date"])


def _run(panel: pl.DataFrame, entry_days: list[int], exit_days: list[int], **matcher):
    dates = [r["date"] for r in panel.select(["symbol", "date"]).iter_rows(named=True)]
    entry_set, exit_set = set(entry_days), set(exit_days)
    entry_mask = pl.Series([(dt - START).days in entry_set for dt in dates], dtype=pl.Boolean)
    exit_mask = pl.Series([(dt - START).days in exit_set for dt in dates], dtype=pl.Boolean)
    kw: dict = {"matching": "close_t", "fees_pct": 0, "slippage_bps": 0}
    kw.update(matcher)
    cfg = MatcherConfig(**kw)
    return BacktestEngine(repo=None).simulate_independent_candidates(  # type: ignore[arg-type]
        panel, entry_mask, exit_mask, cfg,
    )


def _normal_panel(n: int, *, sealed: dict[int, dict] | None = None) -> list[dict]:
    sealed = sealed or {}
    days = []
    for i in range(n):
        if i in sealed:
            spec = sealed[i]
            days.append(_day(i, spec["o"], spec["h"], spec["l"], spec["c"], ld=True))
        else:
            days.append(_normal(i))
    return days


def test_close_t_exit_deferred_past_sealed_limit_down():
    """收盘封跌停当日的计划出场被拦截, 递延到下一可卖日成交。"""
    days = _normal_panel(8, sealed={3: {"o": 10.10, "h": 10.12, "l": 9.09, "c": 9.09}})
    result = _run(_panel(days), entry_days=[0], exit_days=[3])
    assert result.stats.get("n_trades") == 1
    trade = result.trades[0]
    assert trade.entry_date == "2024-01-01"
    assert trade.exit_date == "2024-01-05"  # 封板日次日, 而非封板日 01-04
    assert trade.exit_reason == "signal"
    assert trade.blocked_exit_days == 1


def test_close_t_exit_on_normal_day_fills_same_day():
    """正常日计划出场不受影响(防过度拦截)。"""
    days = _normal_panel(8)
    result = _run(_panel(days), entry_days=[0], exit_days=[3])
    assert result.stats.get("n_trades") == 1
    trade = result.trades[0]
    assert trade.exit_date == "2024-01-04"
    assert trade.blocked_exit_days == 0


def test_intraday_stop_loss_not_blocked_by_sealed_close():
    """日内止损线在封板前已穿越, 按线价当日成交 — 收盘封板不得误拦盘中出场。"""
    days = _normal_panel(6, sealed={2: {"o": 9.70, "h": 9.72, "l": 9.30, "c": 9.00}})
    result = _run(_panel(days), entry_days=[0], exit_days=[], stop_loss_pct=0.05)
    assert result.stats.get("n_trades") == 1
    trade = result.trades[0]
    assert trade.exit_date == "2024-01-03"  # 触发当日
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == 9.5  # 止损线价(10.0 × 0.95), 非跌停收盘价
    assert trade.blocked_exit_days == 0


def test_open_t1_exit_after_sealed_limit_down_signal_day():
    """open_t+1 按次日开盘卖出: 信号日收盘封板不改变次日开盘出场。"""
    days = _normal_panel(8, sealed={2: {"o": 10.10, "h": 10.12, "l": 9.09, "c": 9.09}})
    result = _run(_panel(days), entry_days=[0], exit_days=[2], matching="open_t+1")
    assert result.stats.get("n_trades") == 1
    trade = result.trades[0]
    assert trade.entry_date == "2024-01-02"  # 次日开盘建仓
    assert trade.exit_date == "2024-01-04"  # 信号次日开盘出场
    assert trade.blocked_exit_days == 0


def test_stuck_position_at_window_end_is_recorded():
    """期末仍被封板递延的持仓不再被静默丢弃, 按末日价格强制落账。"""
    days = _normal_panel(8, sealed={7: {"o": 10.30, "h": 10.32, "l": 9.27, "c": 9.27}})
    result = _run(_panel(days), entry_days=[0], exit_days=[7])
    assert result.stats.get("n_trades") == 1
    trade = result.trades[0]
    assert trade.exit_date == "2024-01-08"  # 末日
    assert trade.exit_reason == "signal"
    assert trade.blocked_exit_days >= 1
    assert trade.exit_price == 9.27  # 末日(封板)收盘价
