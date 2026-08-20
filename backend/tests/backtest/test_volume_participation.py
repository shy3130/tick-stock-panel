"""A1 量能参与率约束 (max_participation_pct) 的撮合与容量诊断测试。

覆盖: 配置校验、滚动窗口均值口径、买入股数截断、0 上限阻塞原因、
volume 列缺失/全 0 回退、capacity 统计块字段与分位数口径、
整手取整不算 capped、独立候选路径的 1 手阻塞。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig

CAPACITY_KEYS = {
    "enabled", "capped_entry_count", "cap_value_p50", "cap_value_p10",
    "utilization_p50", "utilization_p90", "unconstrained", "est_capacity_multiple",
}


def _panel(
    symbols: list[str],
    days: int = 4,
    price: float = 10.0,
    overrides: dict[tuple[str, int], dict] | None = None,
    volume: float = 1_000_000.0,
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
                "volume": patch.get("volume", volume),
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
    return BacktestEngine(repo=None)  # simulate_* 不访问 repo


def _config(**kwargs) -> MatcherConfig:
    defaults = dict(
        matching="close_t",
        fees_pct=0,
        slippage_bps=0,
        max_positions=1,
        initial_capital=100_000,
    )
    defaults.update(kwargs)
    return MatcherConfig(**defaults)


# ── 配置校验 ─────────────────────────────────────────


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5, 2.0])
def test_matcher_config_rejects_invalid_participation_pct(bad: float) -> None:
    with pytest.raises(ValueError, match="max_participation_pct"):
        MatcherConfig(max_participation_pct=bad)


def test_matcher_config_rejects_invalid_volume_window() -> None:
    with pytest.raises(ValueError, match="participation_volume_window"):
        MatcherConfig(max_participation_pct=0.1, participation_volume_window=0)


def test_matcher_config_accepts_boundary_values() -> None:
    MatcherConfig()  # 默认关闭
    MatcherConfig(max_participation_pct=0.10)
    MatcherConfig(max_participation_pct=1.0, participation_volume_window=1)


# ── 面板预处理口径 ───────────────────────────────────


def test_disabled_cap_leaves_matching_unchanged() -> None:
    """max_participation_pct=None (默认): 不加约束列, 撮合行为与既有无约束路径一致。"""
    panel = _panel(["A"], days=4, volume=100_000.0)
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(panel, entries, exits, _config())

    assert "_vol_cap_shares" not in panel.columns  # 原面板不被改写
    assert len(result.trades) == 1
    assert result.trades[0].shares == 10_000.0  # 100000 / 10 元, 未受任何量能约束
    assert result.stats["capacity"] == {
        "enabled": False,
        "capped_entry_count": 0,
        "cap_value_p50": None,
        "cap_value_p10": None,
        "utilization_p50": None,
        "utilization_p90": None,
        "unconstrained": None,
        "est_capacity_multiple": None,
    }
    assert "buy_volume_cap" not in result.stats["execution"] or \
        result.stats["execution"]["buy_volume_cap"] == 0


def test_volume_cap_uses_inclusive_window_mean_and_isolates_symbols() -> None:
    """cap = pct × min(当日量, 含当日 window 日简单均值); 均值不跨品种; 单行缺失 → null。"""
    start = date(2024, 1, 1)
    rows = [{"symbol": "A", "date": start + timedelta(days=i), "close": 10.0, "volume": v}
            for i, v in enumerate([1000.0, 2000.0, 6000.0, None])]
    rows.append({"symbol": "B", "date": start, "close": 5.0, "volume": 10000.0})
    panel = pl.DataFrame(rows).sort(["symbol", "date"])

    out = BacktestEngine._with_volume_cap(
        panel, MatcherConfig(max_participation_pct=0.1, participation_volume_window=2)
    )
    caps = out["_vol_cap_shares"].to_list()
    # A: 首行均值即自身 min(1000,1000)=1000; 次行均值 1500 < 2000; 第三日均值 4000 < 6000; 缺失行 null。
    # B: 独立首行, 均值即自身 (不混入 A 的行)。
    assert caps == [0.1 * 1000, 0.1 * 1500, 0.1 * 4000, None, 0.1 * 10000]


def test_volume_cap_all_zero_column_becomes_null_and_disables_cap() -> None:
    panel = _panel(["A"], days=3, volume=0.0)
    out = BacktestEngine._with_volume_cap(panel, _config(max_participation_pct=0.1))
    assert out["_vol_cap_shares"].to_list() == [None] * 3


# ── 组合路径: 截断 / 阻塞 / 回退 ─────────────────────


def test_portfolio_cap_truncates_shares_and_reports_capacity() -> None:
    """目标 10000 股被 cap=5000 股截断; entry_value 同步; capacity 块口径正确。"""
    panel = _panel(["A"], days=4, volume=100_000.0)
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(
        panel, entries, exits,
        _config(max_participation_pct=0.05, participation_volume_window=5),
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.shares == 5_000.0  # cap = 0.05 × min(1e5, 1e5) = 5000 股
    assert trade.entry_value == pytest.approx(50_000.0)
    assert trade.lots == 50.0

    capacity = result.stats["capacity"]
    assert set(capacity.keys()) == CAPACITY_KEYS
    assert capacity["enabled"] is True
    assert capacity["capped_entry_count"] == 1
    assert capacity["cap_value_p50"] == pytest.approx(5_000 * 10.0)
    assert capacity["cap_value_p10"] == pytest.approx(5_000 * 10.0)
    assert capacity["utilization_p50"] == pytest.approx(1.0)
    assert capacity["utilization_p90"] == pytest.approx(1.0)
    assert capacity["unconstrained"] is False
    assert capacity["est_capacity_multiple"] == 1.0
    assert result.stats["execution"]["buy_volume_cap"] == 0  # 被截断 ≠ 被阻塞


def test_zero_volume_day_blocks_buy_with_volume_cap_reason() -> None:
    """成交日 volume=0 (价格有效非停牌) → cap=0 → 买入阻塞并计入 buy_volume_cap。"""
    panel = _panel(
        ["A"],
        days=3,
        overrides={
            # 价格有波动的无量日: 不落入 buy_suspended 的同价停牌判定, 才能命中量能阻塞。
            ("A", 0): {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 0},
        },
    )
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(
        panel, entries, exits,
        _config(max_participation_pct=0.1, participation_volume_window=5),
    )

    assert result.trades == []
    assert result.stats["execution"]["buy_volume_cap"] == 1
    assert result.stats["execution"]["buy_suspended"] == 0
    assert result.stats["capacity"]["enabled"] is True
    assert result.stats["capacity"]["capped_entry_count"] == 0


def test_missing_volume_column_falls_back_to_unconstrained() -> None:
    """volume 列缺失: 撮合回退无约束行为, capacity.enabled=False, 分位数为 null。"""
    panel = _panel(["A"], days=4).drop("volume")
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(
        panel, entries, exits,
        _config(max_participation_pct=0.1),
    )

    assert len(result.trades) == 1
    assert result.trades[0].shares == 10_000.0  # 无约束: 100000 / 10 元全额买入
    capacity = result.stats["capacity"]
    assert set(capacity.keys()) == CAPACITY_KEYS
    assert capacity["enabled"] is False
    assert capacity["capped_entry_count"] == 0
    assert capacity["cap_value_p50"] is None
    assert capacity["cap_value_p10"] is None
    assert capacity["utilization_p50"] is None
    assert capacity["utilization_p90"] is None
    assert capacity["unconstrained"] is None
    assert capacity["est_capacity_multiple"] is None
    assert result.stats["execution"].get("buy_volume_cap", 0) == 0


def test_all_zero_volume_panel_keeps_existing_suspension_behavior() -> None:
    """整列 volume=0: cap 全 null 回退; 无量同价日由既有 buy_suspended 阻塞, 不误报量能原因。"""
    panel = _panel(["A"], days=3, volume=0.0)
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(
        panel, entries, exits,
        _config(max_participation_pct=0.1),
    )

    assert result.trades == []
    assert result.stats["execution"]["buy_suspended"] == 1
    assert result.stats["execution"]["buy_volume_cap"] == 0
    assert result.stats["capacity"]["enabled"] is False


# ── capacity 统计口径 ────────────────────────────────


def test_capacity_percentiles_and_utilization_across_symbols() -> None:
    """两只不同量能: 一笔被截断 (util=1.0) 一笔未触 (util=0.2), 分位数与倍数按笔计算。"""
    panel = _panel(
        ["A", "B"],
        days=3,
        overrides={("A", 0): {"volume": 100_000.0}},
        volume=1_000_000.0,  # B 默认量能
    )
    entries = _mask(panel, {("A", 0), ("B", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(
        panel, entries, exits,
        _config(max_participation_pct=0.05, max_positions=2, initial_capital=200_000),
    )

    assert len(result.trades) == 2
    by_symbol = {t.symbol: t for t in result.trades}
    assert by_symbol["A"].shares == 5_000.0   # cap = 0.05×1e5 = 5000 < 目标 10000
    assert by_symbol["B"].shares == 10_000.0  # cap = 0.05×1e6 = 50000 > 目标 10000

    capacity = result.stats["capacity"]
    assert set(capacity.keys()) == CAPACITY_KEYS
    assert capacity["enabled"] is True
    assert capacity["capped_entry_count"] == 1
    # cap_value: A=5000×10, B=50000×10 → 排序后 [50000, 500000]
    assert capacity["cap_value_p50"] == pytest.approx(275_000.0)
    assert capacity["cap_value_p10"] == pytest.approx(95_000.0)
    # utilization: A=1.0 (触顶), B=0.2 → 排序后 [0.2, 1.0]
    assert capacity["utilization_p50"] == pytest.approx(0.6)
    assert capacity["utilization_p90"] == pytest.approx(0.92)
    assert capacity["unconstrained"] is False  # 存在被截断笔
    assert capacity["est_capacity_multiple"] == pytest.approx(1.09)


def test_lot_rounding_alone_is_not_counted_as_capped() -> None:
    """cap 高于取整前目标: 整手取整造成的截断不计 capped, unconstrained=True。"""
    panel = _panel(["A"], days=3, volume=30_000.0)
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_portfolio(
        panel, entries, exits,
        # 资金 100500 → 取整前目标 10050 股; cap = 0.5×30000 = 15000 > 目标 → 未触约束。
        # 成交取整到 10000 股属于整手截断, 不算 capped。
        _config(max_participation_pct=0.5, initial_capital=100_500),
    )

    assert len(result.trades) == 1
    assert result.trades[0].shares == 10_000.0
    capacity = result.stats["capacity"]
    assert capacity["capped_entry_count"] == 0
    assert capacity["utilization_p90"] == pytest.approx(round(100_000.0 / 150_000.0, 4))
    assert capacity["unconstrained"] is True
    assert capacity["est_capacity_multiple"] == pytest.approx(1.5)


# ── 独立候选 (full) 路径 ────────────────────────────


def test_independent_candidates_zero_cap_blocks_entry_with_reason() -> None:
    panel = _panel(["A"], days=3, volume=500.0)  # cap = 0.1×500 = 50 股 < 1 手
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_independent_candidates(
        panel, entries, exits,
        _config(max_participation_pct=0.1),
    )

    assert result.trades == []
    assert result.stats["execution"]["buy_volume_cap"] == 1
    assert result.stats["capacity"]["enabled"] is True
    assert result.stats["capacity"]["capped_entry_count"] == 0


def test_independent_candidates_capacity_uses_one_lot_sample() -> None:
    """独立候选每笔固定 1 手: util = 100 股名义额 / cap 名义额, 样本进入 capacity 统计。"""
    panel = _panel(["A"], days=3, volume=2_000.0)  # cap = 0.1×2000 = 200 股 ≥ 1 手
    entries = _mask(panel, {("A", 0)})
    exits = _mask(panel, set())

    result = _engine().simulate_independent_candidates(
        panel, entries, exits,
        _config(max_participation_pct=0.1),
    )

    assert len(result.trades) == 1
    assert result.trades[0].shares == 100.0
    capacity = result.stats["capacity"]
    assert capacity["enabled"] is True
    assert capacity["cap_value_p50"] == pytest.approx(200 * 10.0)
    assert capacity["utilization_p90"] == pytest.approx(0.5)
    assert capacity["unconstrained"] is True
    assert capacity["est_capacity_multiple"] == 2.0
