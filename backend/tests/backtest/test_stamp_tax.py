"""印花税显式化 (F3) 测试 — 卖出单边扣费口径与 cost_breakdown 拆分。

口径契约:
- 买入成本 = fees + slippage (不含印花税)
- 卖出成本 = fees + slippage + stamp_tax
- cost_breakdown.stamp_tax 按卖出侧名义额 × stamp_tax_pct, total 纳入
- 印花税为 0 时与旧行为一致
- 成本敏感性倍数不放大印花税
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest import cost_sensitivity as cs
from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.strategy import StrategyBacktestConfig


def _panel(symbols: list[str], days: int = 4, price: float = 10.0) -> pl.DataFrame:
    start = date(2024, 1, 1)
    rows = []
    for sym in symbols:
        for i in range(days):
            rows.append({
                "symbol": sym,
                "name": sym,
                "date": start + timedelta(days=i),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 1_000_000,
                "score": 5.0,
                "signal_limit_up": False,
                "signal_limit_down": False,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _mask(panel: pl.DataFrame, marks: set[tuple[str, int]]) -> pl.Series:
    values = []
    base = date(2024, 1, 1)
    for row in panel.select(["symbol", "date"]).iter_rows(named=True):
        day = (row["date"] - base).days
        values.append((row["symbol"], day) in marks)
    return pl.Series(values, dtype=pl.Boolean)


def _run_portfolio(**kwargs) -> object:
    panel = _panel(["A"])
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, {("A", 2)})
    cfg = MatcherConfig(
        matching="close_t",
        fees_pct=0,
        slippage_bps=0,
        initial_capital=100_000,
        **kwargs,
    )
    return BacktestEngine(repo=None).simulate_portfolio(panel, entries, exits, cfg)


def test_buy_side_excludes_stamp_tax():
    """买入侧名义额不受印花税影响: entry_value = shares × price (无 stamp)。"""
    r0 = _run_portfolio(stamp_tax_pct=0.0)
    r1 = _run_portfolio(stamp_tax_pct=0.001)
    t0, t1 = r0.trades[0], r1.trades[0]
    assert t0.entry_value == pytest.approx(t1.entry_value, rel=1e-9)


def test_sell_side_includes_stamp_tax():
    """卖出侧含印花税: exit_value 随 stamp_tax_pct 递减。"""
    r0 = _run_portfolio(stamp_tax_pct=0.0)
    r1 = _run_portfolio(stamp_tax_pct=0.001)
    shares = r0.trades[0].shares
    price = r0.trades[0].exit_price
    expect = shares * price * (1 - 0.001)
    assert r1.trades[0].exit_value == pytest.approx(expect, rel=1e-6)
    assert r1.trades[0].exit_value < r0.trades[0].exit_value


def test_cost_breakdown_stamp_tax_and_total():
    """cost_breakdown.stamp_tax = 卖出侧名义额 × 税率, total 含之。"""
    r = _run_portfolio(stamp_tax_pct=0.001)
    bd = r.stats["cost_breakdown"]
    exit_notional = r.trades[0].exit_value
    assert bd["stamp_tax"] == pytest.approx(exit_notional * 0.001, abs=0.05)
    assert bd["total"] == pytest.approx(bd["commission"] + bd["slippage"] + bd["stamp_tax"], abs=0.02)


def test_zero_stamp_tax_matches_legacy_behavior():
    """stamp=0 时 cost_breakdown 与旧口径一致: 无 stamp_tax 键值, total = commission+slippage。"""
    r = _run_portfolio(stamp_tax_pct=0.0)
    bd = r.stats["cost_breakdown"]
    assert bd["stamp_tax"] == 0.0
    assert bd["total"] == pytest.approx(bd["commission"] + bd["slippage"], abs=0.02)


def test_candidate_mode_pnl_deducts_stamp_tax():
    """全量独立候选路径: pnl 扣费在双边 fees+slippage 之上多扣一次 stamp。"""
    panel = _panel(["A"])
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, {("A", 2)})
    base = MatcherConfig(
        matching="close_t", fees_pct=0.001, slippage_bps=0,
        stamp_tax_pct=0.0, initial_capital=100_000,
    )
    taxed = MatcherConfig(
        matching="close_t", fees_pct=0.001, slippage_bps=0,
        stamp_tax_pct=0.002, initial_capital=100_000,
    )
    engine = BacktestEngine(repo=None)
    r0 = engine.simulate_independent_candidates(panel, entries, exits, base)
    r1 = engine.simulate_independent_candidates(panel, entries, exits, taxed)
    assert len(r0.trades) >= 1 and len(r1.trades) >= 1
    diff = r0.trades[0].pnl_pct - r1.trades[0].pnl_pct
    # pnl_pct = exit_value/entry_value - 1 (比率口径): stamp 差 = 0.002/(1+fees)
    assert diff == pytest.approx(0.002 / 1.001, rel=1e-4)


@dataclass
class _Result:
    stats: dict = field(default_factory=dict)


def test_cost_sensitivity_multiplier_does_not_scale_stamp_tax():
    """敏感性倍数只乘 fees+slippage, 印花税保持原值。"""
    seen: list[float] = []

    def run_fn(cfg: StrategyBacktestConfig):
        seen.append(float(cfg.stamp_tax_pct))
        return _Result(stats={"total_return": 0.1})

    cfg = StrategyBacktestConfig(
        strategy_id="t", symbols=None,
        start=date(2024, 1, 1), end=date(2024, 6, 30),
        fees_pct=0.0002, slippage_bps=5.0, stamp_tax_pct=0.0005,
    )
    out = cs.run_cost_sensitivity(run_fn, cfg, multipliers=(0.0, 2.0))
    assert out["multipliers"] == [0.0, 1.0, 2.0]
    # 三档印花税均保持 0.0005 不被倍数放大
    assert seen == [0.0005, 0.0005, 0.0005]


def test_matcher_config_default_stamp_tax():
    """默认税率 = 万分之五 (2023-08-28 起 A 股印花税)。"""
    assert MatcherConfig().stamp_tax_pct == 0.0005
