"""持仓期 MAE/MFE (最大不利/有利偏移) 契约测试。

口径契约:
- 可观测窗口按建仓成交口径: entry_fill=open_t+1 (开盘成交) 含入场日区间;
  close_t (收盘成交) 入场日区间发生在成交前 (前视) → 不计入, 自下一交易日起;
  退出日保守不计入。max_high (Trailing Stop) 维持既有语义, 不受本契约约束。
- raw high/low 相对 entry_price 偏移, mae_pct <= 0 / mfe_pct >= 0 (未跌破/涨超钳制为 0)。
- 整个可观测窗口无有效 high/low → None, 不伪造 0; 旧记录缺字段同 None。
- 计算不得改变撮合/资金/成交结果 (exit_reason/exit_price/duration 不受影响)。
"""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import date, timedelta

import polars as pl

from app.backtest import metrics as mt
from app.backtest.engine import BacktestEngine, MatcherConfig, TradeRecord
from app.backtest.strategy import StrategyBacktestService
from app.json_safe import json_safe


def _panel(
    symbols: list[str],
    days: int = 4,
    price: float = 10.0,
    overrides: dict[tuple[str, int], dict] | None = None,
) -> pl.DataFrame:
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
                "score": patch.get("score", 0.0),
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
    return BacktestEngine(repo=None)


def _boundary_panel(symbol: str = "A") -> pl.DataFrame:
    """可观测边界反证面板 (close_t 视角: 入场日 day0 / 持仓日 day1 / 退出日 day2)。

    day0 (close_t 入场日, 收盘成交前) low=9.0/high=10.5 → 不应计入 (前视);
    day1 (close_t 首个可观测日) low=9.5/high=11.0 → 唯一应计入的区间;
    day2 (退出日) low=8.0/high=12.0 全序列极值 → 不应计入 (任一泄漏即暴露)。
    """
    return _panel(
        [symbol],
        days=4,
        overrides={
            (symbol, 0): {"open": 10, "high": 10.5, "low": 9.0, "close": 10},
            (symbol, 1): {"open": 10, "high": 11.0, "low": 9.5, "close": 10.2},
            (symbol, 2): {"open": 10, "high": 12.0, "low": 8.0, "close": 10.5},
        },
    )


def _excursion_config(**extra) -> MatcherConfig:
    return MatcherConfig(
        fees_pct=0,
        slippage_bps=0,
        max_positions=2,
        initial_capital=100_000,
        max_hold_days=2,
        **extra,
    )


def test_portfolio_excursion_close_t_excludes_entry_day_and_exit_day():
    """close_t 收盘成交: 入场日区间发生在成交前 (前视) → 不计入, 首个可观测日为次日。"""
    panel = _boundary_panel()
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(panel, entries, exits, _excursion_config())

    assert len(result.trades) == 1
    trade = result.trades[0]
    # 撮合语义不受 MAE/MFE 计算影响
    assert trade.exit_reason == "max_hold"
    assert trade.duration == 2
    assert trade.exit_price == 10.5
    # 唯一可观测 day1: low=9.5/high=11.0 → mae=-0.05, mfe=+0.1
    # (非 -0.2/12.0 泄漏: day0 前视 excluded, day2 退出日 excluded)
    assert trade.mae_pct == -0.05
    assert trade.mfe_pct == 0.1


def test_full_mode_excursion_close_t_excludes_entry_day_and_exit_day():
    panel = _boundary_panel()
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_independent_candidates(panel, entries, exits, _excursion_config())

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "max_hold"
    assert trade.duration == 2
    assert trade.exit_price == 10.5
    assert trade.mae_pct == -0.05
    assert trade.mfe_pct == 0.1
    # 序列化: dataclass → dict 透出 optional 字段
    payload = asdict(trade)
    assert payload["mae_pct"] == -0.05
    assert payload["mfe_pct"] == 0.1


def _open_fill_panel(symbol: str = "A") -> pl.DataFrame:
    """open_t+1 视角: 信号 day0, day1 开盘成交 (入场日=day1, entry=10), max_hold=2 → day3 退出。

    入场日 day1 low=8.8/high=11.2 是全序列唯一极值 → 若入场日被误排除,
    mae/mfe 会退化为 day2 的 -0.05/+0.05, 断言立即失败;
    day2 (9.5/10.5) 在 day1 区间内; day3 (退出日) 与 day0 (成交前) 区间温和。
    """
    return _panel(
        [symbol],
        days=5,
        overrides={
            (symbol, 0): {"open": 10, "high": 10.2, "low": 9.8, "close": 10},
            (symbol, 1): {"open": 10, "high": 11.2, "low": 8.8, "close": 10.2},
            (symbol, 2): {"open": 10, "high": 10.5, "low": 9.5, "close": 10.3},
            (symbol, 3): {"open": 10, "high": 10.2, "low": 9.8, "close": 10.4},
        },
    )


def test_portfolio_excursion_open_t1_includes_entry_day():
    """open_t+1 当日开盘成交: 入场日区间发生在成交后 → 应计入 (回归防线)。"""
    panel = _open_fill_panel()
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(panel, entries, exits, _excursion_config(matching="open_t+1"))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "max_hold"
    assert trade.duration == 2
    assert trade.entry_price == 10.0  # day1 开盘成交
    assert trade.exit_price == 10.0  # exit_fill 同为 open_t+1 → day3 开盘退出
    # 入场日 day1 是唯一极值: mae=8.8/10-1, mfe=11.2/10-1;
    # 若入场日被误排除 → 退化为 day2 的 -0.05/+0.05, 断言失败
    assert trade.mae_pct == -0.12
    assert trade.mfe_pct == 0.12


def test_full_mode_excursion_open_t1_includes_entry_day():
    panel = _open_fill_panel()
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_independent_candidates(panel, entries, exits, _excursion_config(matching="open_t+1"))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "max_hold"
    assert trade.entry_price == 10.0
    assert trade.mae_pct == -0.12
    assert trade.mfe_pct == 0.12


def test_nonfinite_extremes_yield_null_not_zero():
    nan = float("nan")
    panel = _panel(
        ["A"],
        days=4,
        overrides={
            ("A", 0): {"open": 10, "high": nan, "low": nan, "close": 10},
            ("A", 1): {"open": 10, "high": nan, "low": nan, "close": 10.2},
            ("A", 2): {"open": 10, "high": nan, "low": nan, "close": 10.5},
        },
    )
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(panel, entries, exits, _excursion_config())

    assert len(result.trades) == 1
    trade = result.trades[0]
    # open/close 有效 → 交易正常撮合退出
    assert trade.exit_reason == "max_hold"
    # 整个持仓期无有效 high/low → None, 不伪造 0
    assert trade.mae_pct is None
    assert trade.mfe_pct is None


def test_excursion_clamped_to_zero_when_never_adverse():
    panel = _panel(
        ["A"],
        days=4,
        overrides={
            ("A", 0): {"open": 10, "high": 10.8, "low": 10.2, "close": 10},
            ("A", 1): {"open": 10, "high": 11.0, "low": 10.5, "close": 10.2},
            ("A", 2): {"open": 10, "high": 10.9, "low": 10.4, "close": 10.5},
        },
    )
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(panel, entries, exits, _excursion_config())

    trade = result.trades[0]
    # 持仓期从未低于入场价 → 真实 0.0 (区别于不可得的 None)
    assert trade.mae_pct == 0.0
    assert trade.mfe_pct == 0.1


def test_stats_aggregate_excursions_position_path():
    panel = _panel(
        ["A", "B"],
        days=4,
        overrides={
            ("A", 0): {"high": 10.5, "low": 9.0},
            ("A", 1): {"high": 11.0, "low": 9.5},
            ("A", 2): {"high": 12.0, "low": 8.0, "close": 10.5},
            ("B", 0): {"high": 10.3, "low": 9.8},
            ("B", 1): {"high": 10.4, "low": 9.7},
            ("B", 2): {"high": 11.0, "low": 9.0, "close": 10.2},
        },
    )
    entries = _mask(panel, {("A", 0), ("B", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(panel, entries, exits, _excursion_config())

    assert len(result.trades) == 2
    # close_t: day0 (入场日) 不计入 → A 可观测仅 day1 (9.5/11.0), B 仅 day1 (9.7/10.4)
    by_symbol = {t.symbol: t for t in result.trades}
    assert by_symbol["A"].mae_pct == -0.05
    assert by_symbol["B"].mae_pct == -0.03
    assert by_symbol["A"].mfe_pct == 0.1
    assert by_symbol["B"].mfe_pct == 0.04
    stats = result.stats
    assert math.isclose(stats["avg_mae_pct"], -0.04, abs_tol=1e-9)
    assert math.isclose(stats["worst_mae_pct"], -0.05, abs_tol=1e-9)
    assert math.isclose(stats["avg_mfe_pct"], 0.07, abs_tol=1e-9)
    assert math.isclose(stats["best_mfe_pct"], 0.1, abs_tol=1e-9)


def test_stats_aggregate_excursions_full_path():
    panel = _panel(
        ["A", "B"],
        days=4,
        overrides={
            ("A", 0): {"high": 10.5, "low": 9.0},
            ("A", 1): {"high": 11.0, "low": 9.5},
            ("A", 2): {"high": 12.0, "low": 8.0, "close": 10.5},
            ("B", 0): {"high": 10.3, "low": 9.8},
            ("B", 1): {"high": 10.4, "low": 9.7},
            ("B", 2): {"high": 11.0, "low": 9.0, "close": 10.2},
        },
    )
    entries = _mask(panel, {("A", 0), ("B", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_independent_candidates(panel, entries, exits, _excursion_config())

    assert len(result.trades) == 2
    by_symbol = {t.symbol: t for t in result.trades}
    assert by_symbol["A"].mae_pct == -0.05
    assert by_symbol["B"].mae_pct == -0.03
    stats = result.stats
    assert math.isclose(stats["avg_mae_pct"], -0.04, abs_tol=1e-9)
    assert math.isclose(stats["worst_mae_pct"], -0.05, abs_tol=1e-9)
    assert math.isclose(stats["avg_mfe_pct"], 0.07, abs_tol=1e-9)
    assert math.isclose(stats["best_mfe_pct"], 0.1, abs_tol=1e-9)


def test_metrics_aggregate_is_null_safe():
    res = mt.performance_metrics(
        pnls=[0.01, -0.02],
        maes=[None, -0.1, 0.0],
        mfes=[None, None, None],
        context=mt.MetricContext("daily"),
    )
    # None 元素剔除后聚合
    assert math.isclose(res["avg_mae_pct"], -0.05, abs_tol=1e-9)
    assert math.isclose(res["worst_mae_pct"], -0.1, abs_tol=1e-9)
    # 全 None → 不生成键 (null-safe), 不输出伪 0
    assert "avg_mfe_pct" not in res
    assert "best_mfe_pct" not in res

    res2 = mt.performance_metrics(pnls=[0.01], maes=[None], mfes=[])
    assert res2["status"] == "ok"
    assert "avg_mae_pct" not in res2
    assert "avg_mfe_pct" not in res2

    # 仅传不可用的 maes → 不足以产出任何指标
    assert mt.performance_metrics(maes=[None])["status"] == "insufficient_data"


def test_legacy_trade_record_defaults_to_none_and_serializes():
    """旧调用方构造 TradeRecord 不传 mae/mfe → 默认 None, 序列化层安全降级。"""
    trade = TradeRecord(
        symbol="A",
        entry_date=date(2024, 1, 1),
        exit_date=date(2024, 1, 3),
        entry_price=10.0,
        exit_price=10.5,
        pnl_pct=0.05,
        duration=2,
        exit_reason="signal",
    )

    assert trade.mae_pct is None
    assert trade.mfe_pct is None

    payload = asdict(trade)
    assert payload["mae_pct"] is None
    assert payload["mfe_pct"] is None

    # json_safe (API 出口): None → null
    safe = json_safe(payload)
    assert safe["mae_pct"] is None
    assert safe["mfe_pct"] is None

    # 策略层交易 dict 序列化同样降级
    trade_dict = StrategyBacktestService._trade_to_dict(trade)
    assert trade_dict["mae_pct"] is None
    assert trade_dict["mfe_pct"] is None
