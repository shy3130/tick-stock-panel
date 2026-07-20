from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl

from app.backtest.engine import BacktestEngine, MatcherConfig


def _panel(symbols: list[str], days: int = 4, price: float = 10.0, overrides: dict[tuple[str, int], dict] | None = None) -> pl.DataFrame:
    overrides = overrides or {}
    start = date(2024, 1, 1)
    rows = []
    for sym in symbols:
        for i in range(days):
            patch = overrides.get((sym, i), {})
            rows.append({
                "symbol": sym,
                "name": sym,
                "date": start + timedelta(days=i),
                "open": patch.get("open", price),
                "high": patch.get("high", price),
                "low": patch.get("low", price),
                "close": patch.get("close", price),
                "volume": patch.get("volume", 100_000),
                "score": patch.get("score", {"A": 4, "B": 3, "C": 2, "D": 1}.get(sym, 0)),
                "signal_limit_up": patch.get("signal_limit_up", False),
                "signal_limit_down": patch.get("signal_limit_down", False),
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _mask(panel: pl.DataFrame, marks: set[tuple[str, int]]) -> pl.Series:
    values = []
    base = date(2024, 1, 1)
    for row in panel.select(["symbol", "date"]).iter_rows(named=True):
        day = (row["date"] - base).days
        values.append((row["symbol"], day) in marks)
    return pl.Series(values, dtype=pl.Boolean)


def _engine() -> BacktestEngine:
    return BacktestEngine(repo=None)  # simulate_portfolio 不访问 repo


def test_max_exposure_sets_target_position_and_caps_count():
    panel = _panel(["A", "B", "C", "D"], days=3)
    entries = _mask(panel, {("A", 0), ("B", 0), ("C", 0), ("D", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(
        panel,
        entries,
        exits,
        MatcherConfig(
            matching="open_t+1",
            fees_pct=0,
            slippage_bps=0,
            max_positions=3,
            max_exposure_pct=0.6,
            initial_capital=100_000,
        ),
    )

    assert len(result.trades) == 3
    assert {t.symbol for t in result.trades} == {"A", "B", "C"}
    assert all(abs(t.position_pct - 0.2) < 0.001 for t in result.trades)
    assert result.stats["max_exposure"] <= 0.61


def test_one_price_limit_up_blocks_buy():
    panel = _panel(
        ["A"],
        days=3,
        overrides={
            ("A", 1): {"open": 11, "high": 11, "low": 11, "close": 11, "signal_limit_up": True},
        },
    )
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(
        panel,
        entries,
        exits,
        MatcherConfig(matching="open_t+1", fees_pct=0, slippage_bps=0, max_positions=1, initial_capital=100_000),
    )

    assert result.trades == []
    assert result.stats["execution"]["buy_limit_up"] == 1


def test_failed_open_exit_keeps_slot_and_blocks_replacement_buy():
    panel = _panel(
        ["A", "B", "C", "D"],
        days=4,
        overrides={
            ("A", 2): {"open": 9, "high": 9, "low": 9, "close": 9, "signal_limit_down": True},
        },
    )
    entries = _mask(panel, {
        ("A", 0), ("B", 0), ("C", 0),
        ("D", 1),
    })
    exits = _mask(panel, {("A", 1)})

    result = _engine().simulate_portfolio(
        panel,
        entries,
        exits,
        MatcherConfig(
            matching="open_t+1",
            fees_pct=0,
            slippage_bps=0,
            max_positions=3,
            max_exposure_pct=0.6,
            initial_capital=100_000,
        ),
    )

    assert "D" not in {t.symbol for t in result.trades}
    assert result.stats["execution"]["sell_limit_down"] == 1
    assert result.stats["execution"]["pending_exit"] == 1
    assert result.stats["execution"]["buy_no_slot"] >= 1
    a_trade = next(t for t in result.trades if t.symbol == "A")
    assert a_trade.blocked_exit_days == 1
    assert a_trade.exit_reason == "signal"


def test_trailing_stop_uses_high_water_mark():
    panel = _panel(
        ["A"],
        days=5,
        overrides={
            ("A", 2): {"open": 10, "high": 12, "low": 11.8, "close": 12},
            ("A", 3): {"open": 12, "high": 12, "low": 11.3, "close": 11.3},
        },
    )
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(
        panel,
        entries,
        exits,
        MatcherConfig(
            matching="open_t+1",
            fees_pct=0,
            slippage_bps=0,
            max_positions=1,
            initial_capital=100_000,
            trailing_stop_pct=0.05,
        ),
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "trailing_stop"
    assert trade.exit_price == 11.4


def test_trailing_take_profit_requires_activation():
    panel = _panel(
        ["A"],
        days=5,
        overrides={
            ("A", 2): {"open": 10, "high": 10.8, "low": 10.4, "close": 10.8},
            ("A", 3): {"open": 10.8, "high": 10.8, "low": 10.4, "close": 10.4},
        },
    )
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(
        panel,
        entries,
        exits,
        MatcherConfig(
            matching="open_t+1",
            fees_pct=0,
            slippage_bps=0,
            max_positions=1,
            initial_capital=100_000,
            trailing_take_profit_activate_pct=0.10,
            trailing_take_profit_drawdown_pct=0.03,
        ),
    )

    assert result.trades[0].exit_reason == "end"


def test_trailing_take_profit_exits_after_activation():
    panel = _panel(
        ["A"],
        days=5,
        overrides={
            ("A", 2): {"open": 10, "high": 12, "low": 11.8, "close": 12},
            ("A", 3): {"open": 12, "high": 12, "low": 11.5, "close": 11.5},
        },
    )
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(
        panel,
        entries,
        exits,
        MatcherConfig(
            matching="open_t+1",
            fees_pct=0,
            slippage_bps=0,
            max_positions=1,
            initial_capital=100_000,
            trailing_take_profit_activate_pct=0.10,
            trailing_take_profit_drawdown_pct=0.03,
        ),
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "trailing_take_profit"
    # 纯峰值口径 (跟随 upstream): 触发线 = 峰值价 × (1 - 回撤%) = 12 × 0.97 = 11.64
    assert trade.exit_price == 11.64


def test_score_filter_uses_signal_day_score_range():
    panel = _panel(
        ["A", "B", "C"],
        days=3,
        overrides={
            ("A", 0): {"score": 70},
            ("B", 0): {"score": 80},
            ("C", 0): {"score": 90},
            ("A", 1): {"score": 100},
            ("B", 1): {"score": 1},
            ("C", 1): {"score": 1},
        },
    )
    entries = _mask(panel, {("A", 0), ("B", 0), ("C", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(
        panel,
        entries,
        exits,
        MatcherConfig(
            matching="open_t+1",
            fees_pct=0,
            slippage_bps=0,
            max_positions=3,
            initial_capital=100_000,
            score_min=71,
            score_max=85,
        ),
    )

    assert {t.symbol for t in result.trades} == {"B"}
    assert result.trades[0].entry_score == 80
    assert result.stats["execution"]["buy_score_filter"] == 2


def test_independent_candidates_allow_overlapping_same_symbol_trades():
    panel = _panel(
        ["A"],
        days=5,
        overrides={
            ("A", 0): {"close": 10},
            ("A", 1): {"close": 11},
            ("A", 2): {"close": 12},
            ("A", 3): {"close": 13},
            ("A", 4): {"close": 14},
        },
    )
    entries = _mask(panel, {("A", 0), ("A", 1)})
    exits = _mask(panel, set())

    result = _engine().simulate_independent_candidates(
        panel,
        entries,
        exits,
        MatcherConfig(matching="close_t", fees_pct=0, slippage_bps=0, max_hold_days=2),
    )

    assert result.stats["full_kind"] == "candidate_execution"
    assert result.stats["n_candidates"] == 2
    assert len(result.trades) == 2
    assert [t.entry_date for t in result.trades] == ["2024-01-01", "2024-01-02"]
    assert [t.exit_date for t in result.trades] == ["2024-01-03", "2024-01-04"]
    assert all(t.exit_reason == "max_hold" for t in result.trades)


def test_independent_candidates_apply_stop_loss():
    panel = _panel(
        ["A"],
        days=4,
        overrides={
            ("A", 0): {"close": 10, "low": 10},
            ("A", 1): {"open": 10, "high": 10, "low": 8.9, "close": 9},
        },
    )
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_independent_candidates(
        panel,
        entries,
        exits,
        MatcherConfig(matching="close_t", fees_pct=0, slippage_bps=0, stop_loss_pct=0.1),
    )

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].exit_price == 9.0


def test_signal_exit_takes_priority_over_max_hold():
    """同一日既有卖点信号又到期 → 应按 signal 平仓 (卖点优先于 max_hold 兜底)。"""
    panel = _panel(
        ["A"],
        days=4,
        overrides={
            # day1 次日开盘买入 (open_t+1), 价 10
            ("A", 1): {"open": 10, "high": 10, "low": 10, "close": 10},
            # day2 持有 (hold_days 计到 1)
            ("A", 2): {"open": 11, "high": 11, "low": 11, "close": 11},
            # day3: 既到期 (hold_days=2 >= max_hold_days=2) 又有卖点信号 → signal 优先
            ("A", 3): {"open": 12, "high": 12, "low": 12, "close": 12},
        },
    )
    entries = _mask(panel, {("A", 0)})  # day0 收盘确认 → day1 开盘买
    exits = _mask(panel, {("A", 2)})    # day2 收盘确认卖点 → day3 开盘卖

    result = _engine().simulate_portfolio(
        panel,
        entries,
        exits,
        MatcherConfig(
            matching="open_t+1",
            fees_pct=0,
            slippage_bps=0,
            max_positions=1,
            max_hold_days=2,
            initial_capital=100_000,
        ),
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "signal"
    assert trade.exit_price == 12.0  # 卖点用 day3 开盘 (exit_fill 跟随 matching=open_t+1)


def test_stop_loss_triggers_even_when_expired_in_open_mode():
    """open_t+1 模式下仓位到期且当日破止损 → 应按 stop_loss 平仓 (风控优先于 max_hold)。"""
    panel = _panel(
        ["A"],
        days=4,
        overrides={
            ("A", 1): {"open": 10, "high": 10, "low": 10, "close": 10},
            # day3 开盘跳空跌破止损 (-10%): open=8.9 < 9.0 止损线, low=8.5
            ("A", 3): {"open": 8.9, "high": 8.9, "low": 8.5, "close": 8.7},
        },
    )
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(
        panel,
        entries,
        exits,
        MatcherConfig(
            matching="open_t+1",
            fees_pct=0,
            slippage_bps=0,
            max_positions=1,
            max_hold_days=2,
            stop_loss_pct=0.1,
            initial_capital=100_000,
        ),
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    # 风控盘中触发: 开盘价 8.9 <= 止损线 9.0 → 按开盘价 8.9 成交
    assert trade.exit_price == 8.9


def test_default_fill_is_buy_open_sell_close():
    """拆分口径: 建仓=次日开盘, 清仓=收盘。entry_price 用次日 open, exit_price 用收盘价。"""
    panel = _panel(
        ["A"],
        days=4,
        overrides={
            # day1: 次日开盘买入, 开盘 10
            ("A", 1): {"open": 10, "high": 10.5, "low": 9.5, "close": 10.2},
            # day2: 到期 (max_hold_days=1), 收盘卖
            ("A", 2): {"open": 11, "high": 11, "low": 10, "close": 10.8},
        },
    )
    entries = _mask(panel, {("A", 0)})  # day0 收盘确认
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(
        panel,
        entries,
        exits,
        MatcherConfig(
            entry_fill="open_t+1",
            exit_fill="close_t",
            fees_pct=0,
            slippage_bps=0,
            max_positions=1,
            max_hold_days=1,
            initial_capital=100_000,
        ),
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price == 10.0   # 次日开盘
    assert trade.exit_price == 10.8    # 到期日收盘
    assert trade.exit_reason == "max_hold"


class _MinuteRepo:
    def __init__(self, rows: pl.DataFrame) -> None:
        self.rows = rows

    def get_minute_by_dates(self, symbols, dates, asset_type="stock"):
        return self.rows.filter(pl.col("symbol").is_in(symbols))


def _minute_trigger_panel() -> tuple[pl.DataFrame, pl.Series, pl.Series]:
    panel = _panel(
        ["A"],
        days=4,
        overrides={
            ("A", 2): {"open": 10.1, "high": 10.3, "low": 8.9, "close": 9.0},
            ("A", 3): {"open": 8.8, "high": 9.0, "low": 8.7, "close": 8.9},
        },
    ).with_columns([
        pl.Series("ma20", [10.0, 10.0, 9.95, 9.9]),
        pl.Series("signal_ma20_breakdown", [False, False, True, False]),
    ])
    return panel, _mask(panel, {("A", 0)}), _mask(panel, {("A", 2)})


def test_minute_signal_exit_fills_at_next_minute_open():
    panel, entries, exits = _minute_trigger_panel()
    minute = pl.DataFrame({
        "symbol": ["A", "A", "A"],
        "datetime": [
            datetime(2024, 1, 3, 9, 31),
            datetime(2024, 1, 3, 9, 32),
            datetime(2024, 1, 3, 9, 33),
        ],
        "open": [10.2, 10.1, 9.7],
        "high": [10.3, 10.2, 9.8],
        "low": [10.1, 9.8, 9.6],
        "close": [10.2, 9.9, 9.7],
        "volume": [100.0, 100.0, 100.0],
        "amount": [1020.0, 990.0, 970.0],
    })

    result = BacktestEngine(repo=_MinuteRepo(minute)).simulate_portfolio(
        panel,
        entries,
        exits,
        MatcherConfig(
            entry_fill="open_t+1",
            exit_fill="signal_next_minute",
            minute_fill=True,
            fees_pct=0,
            slippage_bps=0,
            max_positions=1,
            initial_capital=100_000,
        ),
        exit_signal_ids=["signal_ma20_breakdown"],
    )

    assert len(result.trades) == 1
    assert result.trades[0].exit_date == "2024-01-03"
    assert result.trades[0].exit_price == 9.7
    assert result.trades[0].exit_signal_id == "signal_ma20_breakdown"


def test_minute_signal_exit_without_next_bar_falls_back_to_next_open():
    panel, entries, exits = _minute_trigger_panel()
    minute = pl.DataFrame({
        "symbol": ["A"],
        "datetime": [datetime(2024, 1, 3, 15, 0)],
        "open": [9.9],
        "high": [10.0],
        "low": [8.9],
        "close": [9.0],
        "volume": [100.0],
        "amount": [900.0],
    })

    result = BacktestEngine(repo=_MinuteRepo(minute)).simulate_portfolio(
        panel,
        entries,
        exits,
        MatcherConfig(
            entry_fill="open_t+1",
            exit_fill="signal_next_minute",
            minute_fill=True,
            fees_pct=0,
            slippage_bps=0,
            max_positions=1,
            initial_capital=100_000,
        ),
        exit_signal_ids=["signal_ma20_breakdown"],
    )

    assert len(result.trades) == 1
    assert result.trades[0].exit_date == "2024-01-04"
    assert result.trades[0].exit_price == 8.8
    assert result.stats["execution"]["sell_minute_trigger_fallback"] == 1
