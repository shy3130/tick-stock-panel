"""因子分层回测费用/换手口径的数学精确测试。

口径见 ``app/backtest/factor.py`` 模块说明:
- 每个调仓日按目标权重计算: 两向交易额 traded_notional = Σ|w_t − w_prev|,
  首次建仓按 Σ|w_t|, 最后一期追加期末清仓 Σ|w_t|;
- 标准单边换手 turnover = traded_notional / 2 (初始/最终各 0.5, 完全换仓
  一期 1.0);
- 成本率 cost = traded_notional × one_way_cost, one_way_cost = fees_pct +
  slippage_bps / 10000;
- 分组净收益 net = (1 + gross) × (1 − cost) − 1, 正费用恒为负贡献;
- 多空 = 50% 多最高组 + 50% 空最低组, 两腿成本均从净值扣减。

测试面板用因子 1.0 / 2.0 两只标的 (qcut 边界落在两值之间), 保证 Q1/Q2
成员确定; 全并列因子值退化为单组 Q1。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import BacktestEngine
from app.backtest.factor import FactorBacktestService, FactorConfig

FEES_PCT = 0.0002
SLIPPAGE_BPS = 5.0
ONE_WAY = FEES_PCT + SLIPPAGE_BPS / 10000.0  # 0.0007


def _panel(rows: list[tuple[str, int, float, float]], factor: str = "test_factor") -> pl.DataFrame:
    """(symbol, day, close, factor) → 长表 panel, 日期固定 2024-01-<day>。"""
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "date": [date(2024, 1, r[1]) for r in rows],
            "close": [r[2] for r in rows],
            factor: [r[3] for r in rows],
        }
    )


def _service(panel: pl.DataFrame) -> FactorBacktestService:
    engine = BacktestEngine(repo=None)
    engine.load_panel = lambda *a, **kw: panel
    return FactorBacktestService(engine)


def _config(
    weight: str = "equal",
    fees_pct: float = FEES_PCT,
    slippage_bps: float = SLIPPAGE_BPS,
    risk_free_rate: float = 0.0,
) -> FactorConfig:
    return FactorConfig(
        factor_name="test_factor",
        symbols=None,
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        n_groups=2,
        rebalance="daily",
        weight=weight,
        fees_pct=fees_pct,
        slippage_bps=slippage_bps,
        risk_free_rate=risk_free_rate,
    )


# Q1 = 低因子组 (A), Q2 = 高因子组 (B), 成员在两期之间保持不变。
_STATIC_ROWS = [
    ("A", 1, 100.0, 1.0), ("A", 2, 110.0, 1.0), ("A", 3, 121.0, 1.0),
    ("B", 1, 100.0, 2.0), ("B", 2, 90.0, 2.0), ("B", 3, 81.0, 2.0),
]


def test_zero_fees_reproduce_gross_nav():
    """费用为 0 时净值与既有毛收益口径逐分不差, 换手/成本字段照常输出。"""
    result = _service(_panel(_STATIC_ROWS)).run(_config(fees_pct=0.0, slippage_bps=0.0))

    assert result.error is None
    assert result.group_nav == [
        {"date": "2024-01-01", "Q1": 1.1, "Q2": 0.9},
        {"date": "2024-01-02", "Q1": 1.21, "Q2": 0.81},
    ]
    assert result.group_turnover == [
        {"date": "2024-01-01", "Q1": 0.5, "Q2": 0.5},
        {"date": "2024-01-02", "Q1": 0.5, "Q2": 0.5},
    ]
    q1 = next(g for g in result.group_stats if g["label"] == "Q1")
    assert q1["avg_turnover"] == 0.5
    assert q1["total_turnover"] == 1.0
    assert q1["total_cost"] == 0.0
    assert "sortino" in q1
    assert "value_at_risk" in q1


def test_factor_metrics_share_configured_risk_free_context():
    result = _service(_panel(_STATIC_ROWS)).run(
        _config(fees_pct=0.0, slippage_bps=0.0, risk_free_rate=0.03)
    )

    assert result.metric_context["risk_free_rate"] == pytest.approx(0.03)
    assert result.metric_context["return_frequency"] == "daily"
    assert result.long_short_stats["metric_context"]["risk_free_rate"] == pytest.approx(0.03)
    assert all(
        group["metric_context"]["risk_free_rate"] == pytest.approx(0.03)
        for group in result.group_stats
    )


def test_static_group_only_entry_and_exit_costs():
    """无换仓: 仅初始建仓与期末清仓各 0.5 标准换手, 各计一次单边成本。"""
    result = _service(_panel(_STATIC_ROWS)).run(_config())

    assert result.error is None
    # Q1: 每期 traded_notional = 1 (首期建仓; 末期 0 调仓 + 1 清仓)
    expected_q1 = (110 / 100) * (1 - ONE_WAY) * (121 / 110) * (1 - ONE_WAY)
    assert result.group_nav[0]["Q1"] == pytest.approx(round((110 / 100) * (1 - ONE_WAY), 4))
    assert result.group_nav[1]["Q1"] == pytest.approx(round(expected_q1, 4))

    q1 = next(g for g in result.group_stats if g["label"] == "Q1")
    assert q1["avg_turnover"] == 0.5  # 0.5 (建仓) + 0.5 (清仓) 的均值
    assert q1["total_turnover"] == 1.0
    assert q1["total_cost"] == pytest.approx(2 * ONE_WAY, abs=1e-9)

    # 正费用下净值严格低于零费用毛收益路径
    free = _service(_panel(_STATIC_ROWS)).run(_config(fees_pct=0.0, slippage_bps=0.0))
    for row_cost, row_free in zip(result.group_nav, free.group_nav):
        for g in ("Q1", "Q2"):
            assert row_cost[g] < row_free[g]


def test_full_swap_period_turnover_and_cost():
    """完全换仓一期: traded_notional=2 → turnover=1, 成本=2×one_way_cost。"""
    # 价格恒为 100 (毛收益 0), 因子每日互换成员: Q1 = {A}→{B}→{A}
    rows = [
        ("A", 1, 100.0, 1.0), ("A", 2, 100.0, 2.0), ("A", 3, 100.0, 1.0), ("A", 4, 100.0, 1.0),
        ("B", 1, 100.0, 2.0), ("B", 2, 100.0, 1.0), ("B", 3, 100.0, 2.0), ("B", 4, 100.0, 2.0),
    ]
    result = _service(_panel(rows)).run(_config())

    assert result.error is None
    # 建仓 0.5 → 完全换仓 1.0 → 换回+清仓 (2+1)/2 = 1.5
    assert result.group_turnover == [
        {"date": "2024-01-01", "Q1": 0.5, "Q2": 0.5},
        {"date": "2024-01-02", "Q1": 1.0, "Q2": 1.0},
        {"date": "2024-01-03", "Q1": 1.5, "Q2": 1.5},
    ]
    # 毛收益为 0, 净值只被成本压低: Π (1 − traded_t × one_way_cost)
    expected = (1 - ONE_WAY) * (1 - 2 * ONE_WAY) * (1 - 3 * ONE_WAY)
    assert result.group_nav[2]["Q1"] == pytest.approx(round(expected, 4))

    q1 = next(g for g in result.group_stats if g["label"] == "Q1")
    assert q1["total_turnover"] == 3.0
    assert q1["total_cost"] == pytest.approx(6 * ONE_WAY, abs=1e-9)


def _run_long_short(bottom_closes: list[float], top_closes: list[float], zero_fees: bool = False):
    """A = 最低组 (空头腿, 因子 1), B = 最高组 (多头腿, 因子 2)。"""
    rows = [
        ("A", day + 1, bottom_closes[day], 1.0) for day in range(3)
    ] + [
        ("B", day + 1, top_closes[day], 2.0) for day in range(3)
    ]
    if zero_fees:
        return _service(_panel(rows)).run(_config(fees_pct=0.0, slippage_bps=0.0))
    return _service(_panel(rows)).run(_config())


def test_long_short_costs_reduce_both_legs():
    """多空两腿成本均为负贡献: 不论空头腿盈利还是亏损, 成本都压低净值。"""

    # 空头腿盈利: 最低组每期 -50%, 最高组每期 +50% → 多空毛收益 +50%/期
    gain = _run_long_short([100.0, 50.0, 25.0], [100.0, 150.0, 225.0])
    assert gain.error is None
    # 每期每腿 traded=1 (首期建仓; 末期清仓), 多空成本 = 0.5×(owc+owc) = owc
    assert gain.long_short_nav[0]["value"] == pytest.approx(round(1.5 * (1 - ONE_WAY), 4))
    assert gain.long_short_nav[1]["value"] == pytest.approx(round(1.5 * 1.5 * (1 - ONE_WAY) ** 2, 4))
    assert gain.long_short_stats["total_turnover"] == 1.0  # 0.5/期 × 2 期
    assert gain.long_short_stats["avg_turnover"] == 0.5
    assert gain.long_short_stats["total_cost"] == pytest.approx(2 * ONE_WAY, abs=1e-9)
    assert "sortino" in gain.long_short_stats
    assert "tail_ratio" in gain.long_short_stats

    # 空头腿亏损: 最低组每期 +100%, 最高组每期 +50% → 多空毛收益 -25%/期
    lose = _run_long_short([100.0, 200.0, 400.0], [100.0, 150.0, 225.0])
    assert lose.long_short_nav[0]["value"] == pytest.approx(round(0.75 * (1 - ONE_WAY), 4))
    assert lose.long_short_nav[1]["value"] == pytest.approx(round(0.75 * 0.75 * (1 - ONE_WAY) ** 2, 4))

    # 两个方向下, 扣费净值都严格低于零费用净值 —— 成本绝不会被取反变成正贡献
    free_gain = _run_long_short([100.0, 50.0, 25.0], [100.0, 150.0, 225.0], zero_fees=True)
    free_lose = _run_long_short([100.0, 200.0, 400.0], [100.0, 150.0, 225.0], zero_fees=True)
    for costy, free in ((gain, free_gain), (lose, free_lose)):
        for row_cost, row_free in zip(costy.long_short_nav, free.long_short_nav):
            assert row_cost["value"] < row_free["value"]


def test_target_weights_factor_weight_uses_abs_magnitude():
    w = FactorBacktestService._target_weights(np.array([1.0, -2.0, 3.0]), "factor_weight")
    assert w == pytest.approx([1 / 6, 2 / 6, 3 / 6])


def test_target_weights_factor_weight_zero_falls_back_to_equal():
    w = FactorBacktestService._target_weights(np.array([0.0, 0.0, 0.0]), "factor_weight")
    assert w == pytest.approx([1 / 3, 1 / 3, 1 / 3])


def test_target_weights_factor_weight_nonfinite_excluded():
    w = FactorBacktestService._target_weights(np.array([np.nan, 5.0, np.inf]), "factor_weight")
    assert w == pytest.approx([0.0, 1.0, 0.0])


def test_target_weights_equal():
    w = FactorBacktestService._target_weights(np.array([3.0, 7.0]), "equal")
    assert w == pytest.approx([0.5, 0.5])


def test_factor_weight_zero_falls_back_to_equal_in_run():
    """组内因子值全为 0 时 factor_weight 回退等权, 净值/换手与等权完全一致。"""
    rows = [
        (s, day, close, 0.0)
        for day, close in ((1, 10.0), (2, 11.0), (3, 12.0))
        for s in ("A", "B", "C")
    ]
    panel = _panel(rows)
    weighted = _service(panel).run(_config(weight="factor_weight", fees_pct=0.0, slippage_bps=0.0))
    equal = _service(panel).run(_config(weight="equal", fees_pct=0.0, slippage_bps=0.0))

    assert weighted.error is None
    assert weighted.group_nav == equal.group_nav
    assert weighted.group_turnover == equal.group_turnover
    # 全并列因子值 → 单组 Q1, 等权净值 = 毛收益累乘
    assert weighted.group_nav == [
        {"date": "2024-01-01", "Q1": 1.1},
        {"date": "2024-01-02", "Q1": 1.2},
    ]


def test_duplicate_symbol_date_is_deduplicated_before_weight_and_return_math():
    """重复行必须只代表一份持仓，不能让 dict 权重和收益行数脱节。"""
    panel = pl.DataFrame(
        {
            "symbol": ["A", "A", "B"],
            "date": [date(2024, 1, 1)] * 3,
            "_group": ["Q1"] * 3,
            "_next_return": [0.10, 0.90, 0.20],
            "test_factor": [1.0, 9.0, 2.0],
        }
    )

    periods = FactorBacktestService._calc_group_periods(
        panel,
        _config(fees_pct=0.0, slippage_bps=0.0),
    )
    period = periods.periods[("2024-01-01", "Q1")]

    # 后行 A(90%) 覆盖前行 A(10%)，与 B 等权；首次建仓与期末清仓各一次。
    assert period.gross_return == pytest.approx(0.55)
    assert period.traded_notional == pytest.approx(2.0)


def test_missing_group_liquidates_prior_weights_and_records_cost():
    """某分组当期没有可用标的时，不能把旧仓跨期带到它下次重现。"""
    panel = pl.DataFrame(
        {
            "symbol": ["A", "B", "B"],
            "date": [date(2024, 1, 1), date(2024, 1, 1), date(2024, 1, 2)],
            "_group": ["Q1", "Q2", "Q2"],
            "_next_return": [0.0, 0.0, 0.0],
            "test_factor": [1.0, 2.0, 2.0],
        }
    )

    periods = FactorBacktestService._calc_group_periods(panel, _config())

    # Q1 首期建仓（交易额 1），次期缺失时完整平仓（交易额 1）。
    q1_exit = periods.periods[("2024-01-02", "Q1")]
    assert q1_exit.gross_return == 0.0
    assert q1_exit.traded_notional == pytest.approx(1.0)
    assert q1_exit.turnover == pytest.approx(0.5)
    assert q1_exit.cost == pytest.approx(ONE_WAY)
    assert q1_exit.net_return == pytest.approx(-ONE_WAY)
