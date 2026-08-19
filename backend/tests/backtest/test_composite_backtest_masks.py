"""叠加策略回测掩码合并单元测试 — 退出投影/排名融合, 不依赖完整回测引擎。"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.backtest.strategy import StrategyBacktestService as Svc


def _svc() -> Svc:
    """构造一个不调用 __init__ 的 service 实例(仅测试静态/掩码方法)。"""
    return Svc.__new__(Svc)


def _panel(days: int = 4) -> pl.DataFrame:
    """[symbol, date] 排序的单 symbol 面板。"""
    return pl.DataFrame(
        {
            "symbol": ["A"] * days,
            "date": [date(2026, 1, i + 1) for i in range(days)],
            "close": [10.0 + i for i in range(days)],
        }
    )


def test_hold_window_mask_within_max_hold():
    panel = _panel(4)
    entry = pl.Series([True, False, False, False])
    hold = Svc._hold_window_mask(panel, entry, max_hold=2)
    # day0 (entry), day1 在窗口内; day2,day3 超出
    assert hold.to_list() == [True, True, False, False]


def test_hold_window_mask_respects_symbol_boundary():
    panel = pl.DataFrame(
        {
            "symbol": ["A", "A", "B", "B"],
            "date": [date(2026, 1, 1)] * 4,
            "close": [10.0, 11.0, 20.0, 21.0],
        }
    )
    entry = pl.Series([True, False, False, False])
    hold = Svc._hold_window_mask(panel, entry, max_hold=2)
    # A 的窗口不应跨 symbol 到 B
    assert hold.to_list() == [True, True, False, False]


def test_exit_projection_prevents_cross_close():
    panel = pl.DataFrame(
        {
            "symbol": ["A", "A", "A", "B", "B", "B"],
            "date": [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)] * 2,
            "close": [10.0, 11.0, 12.0, 20.0, 21.0, 22.0],
        }
    )
    entry_a = pl.Series([True, False, False, False, False, False])
    entry_b = pl.Series([False, False, False, False, False, False])  # B 从未入场
    exit_b = pl.Series([False, False, False, False, False, True])  # B 的退出信号
    svc = _svc()
    merged = svc._merge_exit_with_projection(panel, [entry_a, entry_b], [pl.Series([False] * 6), exit_b], max_hold=3)
    # B 从未入场 → B 的退出不应触发任何平仓(来源投影)
    assert merged.to_list() == [False, False, False, False, False, False]


def test_exit_projection_allows_own_exit_in_window():
    panel = _panel(3)
    entry = pl.Series([True, False, False])
    exit_ = pl.Series([False, False, True])  # day2 退出, 在 max_hold=3 窗口内
    svc = _svc()
    merged = svc._merge_exit_with_projection(panel, [entry], [exit_], max_hold=3)
    assert merged.to_list() == [False, False, True]


def test_exit_projection_blocks_exit_beyond_max_hold():
    panel = _panel(4)
    entry = pl.Series([True, False, False, False])
    exit_ = pl.Series([False, False, True, False])  # day2, 与 entry 相差 2, max_hold=2 → 不满足 <2
    svc = _svc()
    merged = svc._merge_exit_with_projection(panel, [entry], [exit_], max_hold=2)
    assert merged.to_list() == [False, False, False, False]


def test_composite_ranked_score_weighted():
    panel = pl.DataFrame(
        {
            "symbol": ["A", "A", "B", "B"],
            "date": [date(2026, 1, 1)] * 4,
            "close": [10.0, 11.0, 20.0, 21.0],
        }
    )
    # 同一天: child_a 命中 A(idx0 close10),B(idx2 close20); child_b 仅命中 A
    scores = [
        pl.Series("s", [80.0, 0.0, 90.0, 0.0]),
        pl.Series("s", [50.0, 0.0, 0.0, 0.0]),
    ]
    entries = [
        pl.Series("e", [True, False, True, False]),
        pl.Series("e", [True, False, False, False]),
    ]
    cs = Svc._composite_ranked_score(panel, scores, entries, [0.6, 0.4])
    vals = cs.to_list()
    # A: (0.6*0.0 + 0.4*0.5)/1.0 = 0.2 → 20
    # B: (0.6*1.0)/0.6 = 1.0 → 100
    assert abs(vals[0] - 20.0) < 0.01
    assert abs(vals[2] - 100.0) < 0.01


def test_composite_ranked_score_single_candidate_neutral():
    panel = pl.DataFrame(
        {
            "symbol": ["A"],
            "date": [date(2026, 1, 1)],
            "close": [10.0],
        }
    )
    # 单候选 → 中性分 0.5 → 50
    scores = [pl.Series("s", [80.0])]
    entries = [pl.Series("e", [True])]
    cs = Svc._composite_ranked_score(panel, scores, entries, [1.0])
    assert abs(cs.to_list()[0] - 50.0) < 0.01


def test_composite_ranked_score_multi_date_groups():
    panel = pl.DataFrame(
        {
            "symbol": ["A", "B", "A", "B", "C"],
            "date": [date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 2), date(2026, 1, 2)],
            "close": [10.0, 11.0, 12.0, 13.0, 14.0],
        }
    )
    scores = [
        pl.Series("s", [80.0, 90.0, 70.0, 60.0, 50.0]),
        pl.Series("s", [40.0, 30.0, 20.0, 10.0, 5.0]),
    ]
    entries = [
        pl.Series("e", [True, True, True, False, True]),
        pl.Series("e", [True, False, False, True, True]),
    ]
    cs = Svc._composite_ranked_score(panel, scores, entries, [1.0, 1.0])
    assert len(cs) == 5
    assert all(v == v for v in cs.to_list())
