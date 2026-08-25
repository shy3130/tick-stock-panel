"""F14 分钟级撮合测试 — VWAP 窗口 / 盘中风控 / 回退计数 / 资源与模式 guard。

口径对照 backend/app/backtest/engine.py MinuteExecutionData:
- open 窗口 09:30-09:45 VWAP (open_t+1 成交), tail 窗口 14:45-15:00 VWAP (close_t 成交);
- 窗口量全 0 → open 向后顺延 / tail 向前回溯, 整日零量 → 回退日 K 价并计数;
- 盘中风控: 分钟 low ≤ 风控线以线价成交, high ≥ 止盈线同理, 同分钟双触取不利方向;
- T+1 / 涨跌停拒单 / 整手 / 参与率上限逻辑与日级路径共用不变。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest
from pydantic import ValidationError

from app.backtest.engine import (
    BacktestEngine,
    MatcherConfig,
    MinuteExecutionData,
    build_minute_execution,
)
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService

BASE = date(2024, 1, 1)


# ------------------------------------------------------------------ #
# 数据构造
# ------------------------------------------------------------------ #

def _panel(
    symbols: list[str],
    days: int,
    price: float = 10.0,
    overrides: dict[tuple[str, int], dict] | None = None,
) -> pl.DataFrame:
    """日 K 面板 (对齐 test_engine_portfolio 的形状)。"""
    overrides = overrides or {}
    rows = []
    for sym in symbols:
        for i in range(days):
            patch = overrides.get((sym, i), {})
            rows.append({
                "symbol": sym,
                "name": sym,
                "date": BASE + timedelta(days=i),
                "open": patch.get("open", price),
                "high": patch.get("high", price),
                "low": patch.get("low", price),
                "close": patch.get("close", price),
                "volume": patch.get("volume", 100_000),
                "score": patch.get("score", 1.0),
                "signal_limit_up": patch.get("signal_limit_up", False),
                "signal_limit_down": patch.get("signal_limit_down", False),
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _mask(panel: pl.DataFrame, marks: set[tuple[str, int]]) -> pl.Series:
    values = []
    for row in panel.select(["symbol", "date"]).iter_rows(named=True):
        day = (row["date"] - BASE).days
        values.append((row["symbol"], day) in marks)
    return pl.Series(values, dtype=pl.Boolean)


def _minute_df(
    sym: str,
    day: str,
    bars: list[tuple[int, float, float, float, float, float]],
) -> pl.DataFrame:
    """单标的单日分钟面板; bars = (hhmm, open, high, low, close, volume)。"""
    rows = [{
        "symbol": sym,
        "datetime": f"{day} {hh // 100:02d}:{hh % 100:02d}:00",
        "open": o, "high": h, "low": lo, "close": c, "volume": v,
    } for hh, o, h, lo, c, v in bars]
    return pl.DataFrame(rows)


def _flat_bars(prices: list[tuple[int, float, float]]) -> list[tuple[int, float, float, float, float, float]]:
    """(hhmm, price, volume) → 单价桶 bar (open=high=low=close)。"""
    return [(hh, p, p, p, p, v) for hh, p, v in prices]


def _engine() -> BacktestEngine:
    return BacktestEngine(repo=None)


# ------------------------------------------------------------------ #
# 1. VWAP 窗口计算
# ------------------------------------------------------------------ #

def test_open_window_vwap_volume_weighted():
    """09:30-09:45 窗口 VWAP = Σ(close×volume)/Σvolume, 窗口外价格不参与。"""
    me = build_minute_execution(_minute_df("A", "2024-01-02", _flat_bars([
        (931, 10.0, 100), (940, 11.0, 300), (945, 12.0, 100), (950, 20.0, 1000),
    ])))
    vwap = me.window_vwap("A", "2024-01-02", "open")
    assert vwap is not None
    assert vwap == pytest.approx((10.0 * 100 + 11.0 * 300 + 12.0 * 100) / 500)


def test_tail_window_vwap_volume_weighted():
    """14:45-15:00 窗口 VWAP 同口径; 更早分钟不参与。"""
    me = build_minute_execution(_minute_df("A", "2024-01-02", _flat_bars([
        (1000, 5.0, 1000), (1445, 12.0, 200), (1450, 10.0, 200), (1500, 8.0, 100),
    ])))
    vwap = me.window_vwap("A", "2024-01-02", "tail")
    assert vwap is not None
    assert vwap == pytest.approx((12.0 * 200 + 10.0 * 200 + 8.0 * 100) / 500)


def test_window_zero_volume_defers_to_later_minutes():
    """开盘窗口量全 0 → 顺延到当日后续分钟 (用第一根有量的分钟成交)。"""
    me = build_minute_execution(_minute_df("A", "2024-01-02", _flat_bars([
        (931, 10.0, 0), (945, 11.0, 0), (946, 15.0, 100), (1000, 20.0, 100),
    ])))
    vwap = me.window_vwap("A", "2024-01-02", "open")
    assert vwap == pytest.approx(15.0)


def test_tail_window_zero_volume_defers_to_earlier_minutes():
    """尾盘窗口量全 0 → 向更早分钟回溯 (15:00 后无分钟)。"""
    me = build_minute_execution(_minute_df("A", "2024-01-02", _flat_bars([
        (1430, 9.0, 100), (1445, 10.0, 0), (1500, 11.0, 0),
    ])))
    vwap = me.window_vwap("A", "2024-01-02", "tail")
    assert vwap == pytest.approx(9.0)


def test_full_day_zero_volume_returns_none():
    """整日无分钟量 → 窗口 VWAP 为 None (调用方回退日 K 价)。"""
    me = build_minute_execution(_minute_df("A", "2024-01-02", _flat_bars([
        (931, 10.0, 0), (1500, 11.0, 0),
    ])))
    assert me.window_vwap("A", "2024-01-02", "open") is None
    assert me.window_vwap("A", "2024-01-02", "tail") is None


# ------------------------------------------------------------------ #
# 2. 撮合路径 — VWAP 替换与回退计数
# ------------------------------------------------------------------ #

def _entry_exit_config(**kwargs) -> MatcherConfig:
    defaults = dict(
        matching="close_t",
        fees_pct=0,
        slippage_bps=0,
        stamp_tax_pct=0,
        max_positions=1,
        initial_capital=100_000,
    )
    defaults.update(kwargs)
    return MatcherConfig(**defaults)


def test_close_t_fills_at_tail_window_vwap():
    """close_t 成交口径: 建仓/清仓价 = 信号日 14:45-15:00 VWAP。"""
    panel = _panel(["A"], days=3, overrides={
        ("A", 0): {"close": 10.0},
        ("A", 2): {"close": 10.0},
    })
    day0 = _minute_df("A", str(BASE), _flat_bars([(1445, 10.0, 100), (1500, 10.0, 100)]))
    day2 = _minute_df("A", str(BASE + timedelta(days=2)), _flat_bars([
        (1445, 11.0, 300), (1500, 13.0, 100),
    ]))
    me = build_minute_execution(pl.concat([day0, day2]))
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0)}), _mask(panel, {("A", 2)}),
        _entry_exit_config(), minute_data=me,
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price == pytest.approx(10.0)
    # 尾盘 VWAP = (11*300 + 13*100) / 400 = 11.5
    assert trade.exit_price == pytest.approx(11.5)
    assert result.stats["bar_precision"] == "minute"
    assert result.stats["minute_fallback_daily"] == 0


def test_open_t_plus_1_fills_at_open_window_vwap():
    """open_t+1 成交口径: 次日 09:30-09:45 VWAP 替换次日开盘价。"""
    panel = _panel(["A"], days=3, overrides={
        ("A", 1): {"open": 10.0},
    })
    day1 = _minute_df("A", str(BASE + timedelta(days=1)), _flat_bars([
        (931, 12.0, 300), (945, 16.0, 100),
    ]))
    me = build_minute_execution(day1)
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0)}), _mask(panel, set()),
        _entry_exit_config(matching="open_t+1", max_hold_days=1), minute_data=me,
    )
    assert len(result.trades) == 1
    # 开盘窗口 VWAP = (12*300 + 16*100)/400 = 13
    assert result.trades[0].entry_price == pytest.approx(13.0)


def test_full_day_zero_volume_falls_back_to_daily_price_and_counts():
    """整日零量 → 回退日 K 价, minute_fallback_daily 显式计数 (不静默)。"""
    panel = _panel(["A"], days=3, overrides={
        ("A", 0): {"close": 10.0},
        ("A", 2): {"close": 10.0},
    })
    day0 = _minute_df("A", str(BASE), _flat_bars([(1445, 10.0, 100), (1500, 10.0, 100)]))
    day2 = _minute_df("A", str(BASE + timedelta(days=2)), _flat_bars([
        (1445, 12.0, 0), (1500, 13.0, 0),
    ]))
    me = build_minute_execution(pl.concat([day0, day2]))
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0)}), _mask(panel, {("A", 2)}),
        _entry_exit_config(), minute_data=me,
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_price == pytest.approx(10.0)  # 日 K close
    assert result.stats["execution"]["sell_minute_fallback"] == 1
    assert result.stats["minute_fallback_daily"] == 1


def test_missing_minute_day_falls_back_and_counts():
    """卖出日完全缺分钟数据 → 正常到期卖出用日 K 价并计数。"""
    panel = _panel(["A"], days=3, overrides={
        ("A", 0): {"close": 10.0},
        ("A", 2): {"close": 9.0},
    })
    day0 = _minute_df("A", str(BASE), _flat_bars([(1445, 10.0, 100), (1500, 10.0, 100)]))
    me = build_minute_execution(day0)  # day2 无分钟数据
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0)}), _mask(panel, {("A", 2)}),
        _entry_exit_config(), minute_data=me,
    )
    assert len(result.trades) == 1
    assert result.trades[0].exit_price == pytest.approx(9.0)
    assert result.stats["minute_fallback_daily"] == 1
    assert result.stats["execution"]["sell_minute_fallback"] == 1


def test_missing_minute_column_builds_empty_and_falls_back():
    """分钟面板缺列 → build_minute_execution 返回空对象, 全部回退日 K 价。"""
    bad = pl.DataFrame({"symbol": ["A"], "datetime": ["2024-01-01 09:31:00"], "close": [10.0]})
    me = build_minute_execution(bad)
    assert me.has_bars("A", "2024-01-01") is False
    panel = _panel(["A"], days=3, overrides={
        ("A", 0): {"close": 10.0},
        ("A", 2): {"close": 10.0},
    })
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0)}), _mask(panel, {("A", 2)}),
        _entry_exit_config(), minute_data=me,
    )
    assert len(result.trades) == 1
    assert result.trades[0].exit_price == pytest.approx(10.0)
    assert result.stats["minute_fallback_daily"] == 2  # 买入 + 卖出各 1


# ------------------------------------------------------------------ #
# 3. 盘中风控
# ------------------------------------------------------------------ #

def test_intraday_stop_loss_fills_at_stop_price():
    """盘中 low ≤ 止损线 → 以止损价成交 (非收盘价)。"""
    panel = _panel(["A"], days=3, overrides={
        ("A", 0): {"close": 10.0},
        ("A", 1): {"low": 9.3, "close": 9.4},
    })
    day1 = _minute_df("A", str(BASE + timedelta(days=1)), [
        (931, 9.8, 9.8, 9.8, 9.8, 100),  # 开盘高于止损线
        (1000, 9.6, 9.6, 9.3, 9.3, 100),  # 盘中跌破 (open 仍高于线)
    ])
    me = build_minute_execution(day1)
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0)}), _mask(panel, set()),
        _entry_exit_config(stop_loss_pct=0.05), minute_data=me,
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(9.5)  # 止损线价, 非分钟 close 9.3


def test_intraday_gap_open_fills_at_minute_open():
    """跳空: 分钟 open 已低于止损线 → 以该分钟 open 成交 (保守取更差价)。"""
    panel = _panel(["A"], days=3, overrides={
        ("A", 0): {"close": 10.0},
        ("A", 1): {"open": 9.0, "low": 8.8, "close": 9.2},
    })
    day1 = _minute_df("A", str(BASE + timedelta(days=1)), [
        (931, 9.0, 9.0, 8.9, 8.9, 100),
    ])
    me = build_minute_execution(day1)
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0)}), _mask(panel, set()),
        _entry_exit_config(stop_loss_pct=0.05), minute_data=me,
    )
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].exit_price == pytest.approx(9.0)  # 分钟 open


def test_intraday_take_profit_fills_at_tp_line():
    panel = _panel(["A"], days=3, overrides={
        ("A", 0): {"close": 10.0},
        ("A", 1): {"high": 12.0, "close": 11.5},
    })
    day1 = _minute_df("A", str(BASE + timedelta(days=1)), [
        (1000, 10.5, 10.5, 10.5, 10.5, 100),
        (1030, 10.8, 12.0, 10.8, 12.0, 100),  # open 低于止盈线, 盘中穿越
    ])
    me = build_minute_execution(day1)
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0)}), _mask(panel, set()),
        _entry_exit_config(take_profit_pct=0.1), minute_data=me,
    )
    assert result.trades[0].exit_reason == "take_profit"
    assert result.trades[0].exit_price == pytest.approx(11.0)  # 止盈线


def test_dual_trigger_same_minute_takes_adverse_direction():
    """同一分钟 low 触止损且 high 触止盈 → 保守按下行风控离场 (不利方向)。"""
    panel = _panel(["A"], days=3, overrides={
        ("A", 0): {"close": 10.0},
        ("A", 1): {"high": 11.5, "low": 9.2, "close": 9.4},
    })
    day1 = _minute_df("A", str(BASE + timedelta(days=1)), [
        # 单根分钟同时穿越止损线 9.5 与止盈线 11 (open 位于两线之间)
        (1000, 10.0, 11.5, 9.2, 9.4, 100),
    ])
    me = build_minute_execution(day1)
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0)}), _mask(panel, set()),
        _entry_exit_config(stop_loss_pct=0.05, take_profit_pct=0.1), minute_data=me,
    )
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].exit_price == pytest.approx(9.5)


# ------------------------------------------------------------------ #
# 4. 与日级结果差异方向 + 共用约束不变
# ------------------------------------------------------------------ #

def test_minute_vs_daily_difference_direction_reasonable():
    """同数据下: 尾盘 VWAP 高于日 K close → 分钟模式卖出价更高、收益更高;
    信号与持有窗口完全一致 (差异只来自成交价口径)。"""
    panel = _panel(["A"], days=3, overrides={
        ("A", 0): {"close": 10.0},
        ("A", 2): {"close": 10.0},
    })
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, {("A", 2)})
    cfg = _entry_exit_config()

    daily = _engine().simulate_portfolio(panel, entries, exits, cfg)
    assert daily.stats["bar_precision"] == "daily"
    assert daily.trades[0].exit_price == pytest.approx(10.0)

    day0 = _minute_df("A", str(BASE), _flat_bars([(1445, 10.0, 100), (1500, 10.0, 100)]))
    day2 = _minute_df("A", str(BASE + timedelta(days=2)), _flat_bars([
        (1445, 11.0, 300), (1500, 13.0, 100),
    ]))
    minute = _engine().simulate_portfolio(
        panel, entries, exits, cfg, minute_data=build_minute_execution(pl.concat([day0, day2])),
    )
    assert minute.trades[0].entry_date == daily.trades[0].entry_date
    assert minute.trades[0].exit_date == daily.trades[0].exit_date
    assert minute.trades[0].exit_price > daily.trades[0].exit_price
    assert minute.trades[0].pnl_pct > daily.trades[0].pnl_pct


def test_t_plus_one_blocks_same_day_reentry_under_minute_mode():
    """T+1 语义保留: 当日卖出释放的标的当日不能买回 (buy_same_day_reentry)。"""
    panel = _panel(["A"], days=4, overrides={
        ("A", 0): {"close": 10.0},
        ("A", 2): {"close": 10.0},
    })
    entries = _mask(panel, {("A", 0), ("A", 2)})  # day2 卖出同时再出买点
    exits = _mask(panel, {("A", 2)})
    days = [
        _minute_df("A", str(BASE + timedelta(days=i)), _flat_bars([
            (1445, 10.0, 100), (1500, 10.0, 100),
        ]))
        for i in range(4)
    ]
    result = _engine().simulate_portfolio(
        panel, entries, exits, _entry_exit_config(),
        minute_data=build_minute_execution(pl.concat(days)),
    )
    assert len(result.trades) == 1  # 只有一笔, 未发生当日回补
    assert result.stats["execution"]["buy_same_day_reentry"] == 1


def test_limit_up_blocks_buy_under_minute_mode():
    """涨跌停拒单保留: 一字涨停日 (日 K 判定) 买入阻塞, 即便分钟 VWAP 有效。"""
    panel = _panel(["A"], days=3, overrides={
        ("A", 0): {"close": 10.0},
        ("A", 1): {"open": 11, "high": 11, "low": 11, "close": 11, "signal_limit_up": True},
    })
    day1 = _minute_df("A", str(BASE + timedelta(days=1)), _flat_bars([
        (931, 11.0, 100), (945, 11.0, 100),
    ]))
    result = _engine().simulate_portfolio(
        panel, _mask(panel, {("A", 0)}), _mask(panel, set()),
        _entry_exit_config(matching="open_t+1"), minute_data=build_minute_execution(day1),
    )
    assert result.trades == []
    assert result.stats["execution"]["buy_limit_up"] == 1


# ------------------------------------------------------------------ #
# 5. MinuteExecutionData 单元行为
# ------------------------------------------------------------------ #

def test_apply_fill_prices_marks_fallback_rows():
    """apply_fill_prices: 有效 VWAP 行替换, 缺数据行保留日 K 价并置回退标记。"""
    me = build_minute_execution(
        _minute_df("A", "2024-01-02", _flat_bars([(931, 12.0, 100), (945, 12.0, 100)]))
    )
    symbols = np.array(["A", "A"])
    dates = np.array(["2024-01-02", "2024-01-03"])
    base = np.array([10.0, 10.0])
    out, flags = me.apply_fill_prices(symbols, dates, base, "open")
    assert out[0] == pytest.approx(12.0)
    assert out[1] == pytest.approx(10.0)
    assert flags.tolist() == [False, True]


def test_intraday_risk_exit_requires_minute_bars():
    """无当日分钟数据时 intraday_risk_exit 返回 None (由日级路径接管)。"""
    me = build_minute_execution(_minute_df("A", "2024-01-02", _flat_bars([(931, 10.0, 100)])))
    hit = me.intraday_risk_exit("A", "2024-01-03", {"entry_price": 10.0}, _entry_exit_config(stop_loss_pct=0.05))
    assert hit is None


# ------------------------------------------------------------------ #
# 6. Service 层 guard
# ------------------------------------------------------------------ #

class _StrategyEngineStub:
    def __init__(self) -> None:
        self.strategy = _stub_strategy()

    def get(self, strategy_id: str):
        return self.strategy


def _stub_strategy():
    from app.strategy.engine import StrategyDef

    return StrategyDef(
        meta={"id": "t", "name": "t", "scoring": {}, "params": [], "limit": 100},
        basic_filter={"enabled": True, "amount_min": 0.0},
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


class _LoadPanelStub:
    """load_panel 直接返回构造面板的 engine stub。"""

    def __init__(self, panel: pl.DataFrame) -> None:
        self.panel = panel

    def load_panel(self, symbols, start, end) -> pl.DataFrame:
        return self.panel


def _service(panel: pl.DataFrame) -> StrategyBacktestService:
    svc = StrategyBacktestService.__new__(StrategyBacktestService)  # noqa: SLF001
    svc.engine = _LoadPanelStub(panel)
    svc.strategy_engine = _StrategyEngineStub()
    return svc


def _svc_config(**kwargs) -> StrategyBacktestConfig:
    defaults = dict(
        strategy_id="t",
        symbols=["A"],
        start=BASE,
        end=BASE + timedelta(days=2),
        matching="close_t",
        fees_pct=0,
        slippage_bps=0,
        max_positions=1,
        initial_capital=100_000,
    )
    defaults.update(kwargs)
    return StrategyBacktestConfig(**defaults)


def _empty_minute_data() -> MinuteExecutionData:
    return build_minute_execution(None)


def test_service_rejects_candidate_mode_with_minute():
    """mode=full + minute → 中文 error (不支持, 文档化边界)。"""
    svc = _service(_panel(["A"], days=3))
    result = svc.run(_svc_config(mode="full", bar_precision="minute"), minute_data=_empty_minute_data())
    assert result.error is not None
    assert "全量候选" in result.error


def test_service_rejects_minute_without_data():
    """bar_precision=minute 但调用路径未提供分钟数据 → fail-closed 报错。"""
    svc = _service(_panel(["A"], days=3))
    result = svc.run(_svc_config(bar_precision="minute"))
    assert result.error is not None
    assert "分钟执行数据" in result.error


def test_service_rejects_invalid_precision_value():
    """枚举外 bar_precision → 中文 error。"""
    svc = _service(_panel(["A"], days=3))
    result = svc.run(_svc_config(bar_precision="hourly"))
    assert result.error is not None
    assert "bar_precision" in result.error


def test_service_resource_guard_rejects_over_100_symbols():
    """标的数 >100 → 资源 guard 中文报错, 不启动计算。"""
    symbols = [f"S{i:03d}" for i in range(101)]
    panel = pl.DataFrame({
        "symbol": symbols,
        "date": [BASE] * len(symbols),
        "open": [10.0] * len(symbols),
        "high": [10.0] * len(symbols),
        "low": [10.0] * len(symbols),
        "close": [10.0] * len(symbols),
        "volume": [1000] * len(symbols),
    })
    svc = _service(panel)
    result = svc.run(
        _svc_config(symbols=symbols, bar_precision="minute"),
        minute_data=_empty_minute_data(),
    )
    assert result.error is not None
    assert "资源上限" in result.error
    assert "101" in result.error


def test_service_resource_guard_rejects_over_120_days():
    """正式区间交易日 >120 → 资源 guard 中文报错。"""
    days = 121
    panel = _panel(["A"], days=days)
    svc = _service(panel)
    result = svc.run(
        _svc_config(end=BASE + timedelta(days=days - 1), bar_precision="minute"),
        minute_data=_empty_minute_data(),
    )
    assert result.error is not None
    assert "资源上限" in result.error
    assert "121" in result.error


# ------------------------------------------------------------------ #
# 7. API schema 校验 (422)
# ------------------------------------------------------------------ #

def test_api_request_rejects_minute_with_full_mode():
    """StrategyBacktestRequest: bar_precision=minute + mode=full → ValidationError (422)。"""
    from app.api.backtest import StrategyBacktestRequest

    with pytest.raises(ValidationError):
        StrategyBacktestRequest(strategy_id="t", mode="full", bar_precision="minute")


def test_api_request_rejects_unknown_precision():
    """bar_precision 枚举外值 → ValidationError (422)。"""
    from app.api.backtest import StrategyBacktestRequest

    with pytest.raises(ValidationError):
        StrategyBacktestRequest(strategy_id="t", bar_precision="hourly")
    req = StrategyBacktestRequest(strategy_id="t")
    assert req.bar_precision == "daily"
