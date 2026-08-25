"""Signal Scorecard 评估引擎测试 — 纯函数分类 / 边界 / 前向窗口 / engine 隔离。"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.services import signal_scorecard_eval as ev
from app.services.signal_scorecard_store import ENGINE_VERSION


def _bar(d: str, o: float, h: float, low: float, c: float) -> dict:
    return {"date": d, "open": o, "high": h, "low": low, "close": c}


def _event(anchor: float = 100.0, direction: str = "up", kind: str = "builtin") -> dict:
    return {
        "id": "e1",
        "anchor_price": anchor,
        "direction_expected": direction,
        "signal_kind": kind,
        "symbol": "600519.SH",
        "date": "2026-08-04",
    }


# ── direction_for_signal ─────────────────────────────────
def test_direction_builtin_defaults_up():
    assert ev.direction_for_signal("builtin") == "up"
    assert ev.direction_for_signal("entry") == "up"


def test_direction_exit_is_not_up():
    assert ev.direction_for_signal("exit") == "not_up"


def test_direction_override_wins():
    assert ev.direction_for_signal("exit", "up") == "up"
    assert ev.direction_for_signal("builtin", "not_up") == "not_up"


def test_direction_override_invalid_falls_back():
    assert ev.direction_for_signal("builtin", "sideways") == "up"
    assert ev.direction_for_signal("exit", None) == "not_up"


# ── evaluate_outcome: up 方向 ────────────────────────────
def test_up_hit_above_band():
    # anchor=100, end=105 → +5% ≥ +2% → hit
    bars = [_bar("2026-08-05", 100.0, 105.0, 99.0, 105.0)]
    r = ev.evaluate_outcome(_event(100.0, "up"), bars, 1)
    assert r["eval_status"] == "completed"
    assert r["outcome"] == "hit"
    assert r["direction_correct"] is True
    assert r["stock_return_pct"] == 5.0


def test_up_miss_below_band():
    # anchor=100, end=95 → -5% ≤ -2% → miss
    bars = [_bar("2026-08-05", 100.0, 101.0, 94.0, 95.0)]
    r = ev.evaluate_outcome(_event(100.0, "up"), bars, 1)
    assert r["outcome"] == "miss"
    assert r["direction_correct"] is False


def test_up_neutral_within_band():
    # anchor=100, end=101 → +1% (|1%| < 2%) → neutral
    bars = [_bar("2026-08-05", 100.0, 101.5, 99.5, 101.0)]
    r = ev.evaluate_outcome(_event(100.0, "up"), bars, 1)
    assert r["outcome"] == "neutral"


# ── ±2% 边界 ─────────────────────────────────────────────
def test_boundary_exactly_plus_two_is_hit():
    # return% == +2.0 → ≥ band → hit (闭区间)
    bars = [_bar("2026-08-05", 0, 0, 0, 102.0)]
    r = ev.evaluate_outcome(_event(100.0, "up"), bars, 1)
    assert r["outcome"] == "hit"


def test_boundary_exactly_minus_two_is_miss():
    bars = [_bar("2026-08-05", 0, 0, 0, 98.0)]
    r = ev.evaluate_outcome(_event(100.0, "up"), bars, 1)
    assert r["outcome"] == "miss"


def test_boundary_just_below_plus_two_is_neutral():
    bars = [_bar("2026-08-05", 0, 0, 0, 101.99)]
    r = ev.evaluate_outcome(_event(100.0, "up"), bars, 1)
    assert r["outcome"] == "neutral"


def test_boundary_just_above_minus_two_is_neutral():
    bars = [_bar("2026-08-05", 0, 0, 0, 98.01)]
    r = ev.evaluate_outcome(_event(100.0, "up"), bars, 1)
    assert r["outcome"] == "neutral"


# ── evaluate_outcome: not_up 方向 ────────────────────────
def test_not_up_hit_when_flat_or_down():
    # +1% ≤ +2% → hit
    bars = [_bar("2026-08-05", 0, 0, 0, 101.0)]
    r = ev.evaluate_outcome(_event(100.0, "not_up"), bars, 1)
    assert r["outcome"] == "hit"
    # -5% ≤ +2% → hit
    bars2 = [_bar("2026-08-05", 0, 0, 0, 95.0)]
    r2 = ev.evaluate_outcome(_event(100.0, "not_up"), bars2, 1)
    assert r2["outcome"] == "hit"


def test_not_up_miss_when_strong_up():
    # +5% > +2% → miss
    bars = [_bar("2026-08-05", 0, 0, 0, 105.0)]
    r = ev.evaluate_outcome(_event(100.0, "not_up"), bars, 1)
    assert r["outcome"] == "miss"
    assert r["direction_correct"] is False


def test_not_up_boundary_exactly_plus_two_is_hit():
    bars = [_bar("2026-08-05", 0, 0, 0, 102.0)]
    r = ev.evaluate_outcome(_event(100.0, "not_up"), bars, 1)
    assert r["outcome"] == "hit"


# ── 多日窗口 (horizon>1) ─────────────────────────────────
def test_multi_day_window_uses_nth_close():
    # anchor=100, T+3 close=108 → +8% → hit; 中间日不影响
    bars = [
        _bar("2026-08-05", 100.0, 101.0, 99.0, 101.0),
        _bar("2026-08-06", 101.0, 104.0, 100.0, 104.0),
        _bar("2026-08-07", 104.0, 109.0, 103.0, 108.0),
    ]
    r = ev.evaluate_outcome(_event(100.0, "up"), bars, 3)
    assert r["outcome"] == "hit"
    assert r["stock_return_pct"] == 8.0
    assert r["end_close"] == 108.0
    assert r["start_price"] == 100.0  # 首根 bar open
    assert r["max_high"] == 109.0
    assert r["min_low"] == 99.0


# ── insufficient forward bars ────────────────────────────
def test_insufficient_forward_bars_is_unable():
    bars = [_bar("2026-08-05", 100.0, 101.0, 99.0, 101.0)]
    r = ev.evaluate_outcome(_event(100.0, "up"), bars, 3)  # 要 3 天只有 1 天
    assert r["eval_status"] == "unable"
    assert r["unable_reason"] == "insufficient_forward_bars"
    assert r["outcome"] is None


def test_none_forward_bars_is_unable():
    r = ev.evaluate_outcome(_event(100.0, "up"), None, 1)
    assert r["eval_status"] == "unable"
    assert r["unable_reason"] == "forward_window_query_failed"


def test_invalid_anchor_is_unable():
    bars = [_bar("2026-08-05", 100.0, 101.0, 99.0, 101.0)]
    r = ev.evaluate_outcome(_event(0.0, "up"), bars, 1)
    assert r["eval_status"] == "unable"
    assert r["unable_reason"] == "invalid_anchor_price"
    r2 = ev.evaluate_outcome(_event(None, "up"), bars, 1)  # type: ignore[arg-type]
    assert r2["unable_reason"] == "invalid_anchor_price"


# ── compute_forward_window ───────────────────────────────
class _FakeRepo:
    """模拟 KlineRepository.get_enriched_range。"""

    def __init__(self, df: pl.DataFrame | None):
        self._df = df
        self.calls: list = []

    def get_enriched_range(self, start, end, symbols=None, columns=None):
        self.calls.append((start, end, symbols, columns))
        return self._df


def test_compute_forward_window_takes_n_trading_days():
    df = pl.DataFrame({
        "symbol": ["A"] * 5,
        "date": [date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7),
                 date(2026, 8, 8), date(2026, 8, 9)],
        "open": [10.0] * 5, "high": [11.0] * 5, "low": [9.0] * 5, "close": [10.5] * 5,
    })
    repo = _FakeRepo(df)
    rows = ev.compute_forward_window(repo, "A", date(2026, 8, 4), 3)
    assert rows is not None
    assert len(rows) == 3
    assert [r["date"] for r in rows] == ["2026-08-05", "2026-08-06", "2026-08-07"]


def test_compute_forward_window_skips_non_trading_gaps():
    # 周末缺口: 8/5(周三) 8/6(周四) 8/9(周日跳过→下周一 8/10)
    # 这里模拟 enriched 只有交易日行, distinct date 升序取前 N 自然跳过缺口
    df = pl.DataFrame({
        "symbol": ["A", "A", "A"],
        "date": [date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 10)],
        "open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0],
        "low": [1.0, 2.0, 3.0], "close": [1.0, 2.0, 3.0],
    })
    repo = _FakeRepo(df)
    rows = ev.compute_forward_window(repo, "A", date(2026, 8, 4), 3)
    assert rows is not None
    assert len(rows) == 3
    assert rows[2]["date"] == "2026-08-10"  # 第 3 个交易日 = 周一


def test_compute_forward_window_insufficient_returns_fewer():
    df = pl.DataFrame({
        "symbol": ["A", "A"],
        "date": [date(2026, 8, 5), date(2026, 8, 6)],
        "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0], "close": [1.0, 2.0],
    })
    repo = _FakeRepo(df)
    rows = ev.compute_forward_window(repo, "A", date(2026, 8, 4), 5)
    assert rows is not None
    assert len(rows) == 2  # 不足 5, 返回实际有的


def test_compute_forward_window_query_failure_returns_none():
    class _BoomRepo:
        def get_enriched_range(self, *a, **k):
            raise RuntimeError("duckdb boom")
    rows = ev.compute_forward_window(_BoomRepo(), "A", date(2026, 8, 4), 3)
    assert rows is None


def test_compute_forward_window_none_df_returns_none():
    repo = _FakeRepo(None)
    assert ev.compute_forward_window(repo, "A", date(2026, 8, 4), 3) is None


def test_compute_forward_window_empty_df_returns_empty():
    repo = _FakeRepo(pl.DataFrame({
        "symbol": [], "date": [], "open": [], "high": [], "low": [], "close": [],
    }))
    rows = ev.compute_forward_window(repo, "A", date(2026, 8, 4), 3)
    assert rows == []


def test_compute_forward_window_filters_other_symbols():
    df = pl.DataFrame({
        "symbol": ["A", "B", "A"],
        "date": [date(2026, 8, 5), date(2026, 8, 5), date(2026, 8, 6)],
        "open": [1.0, 99.0, 2.0], "high": [1.0, 99.0, 2.0],
        "low": [1.0, 99.0, 2.0], "close": [1.0, 99.0, 2.0],
    })
    repo = _FakeRepo(df)
    rows = ev.compute_forward_window(repo, "A", date(2026, 8, 4), 2)
    assert all(r["close"] in (1.0, 2.0) for r in rows)
    assert 99.0 not in [r["close"] for r in rows]


# ── engine_version 隔离 (常量) ───────────────────────────
def test_engine_version_is_stable_string():
    assert isinstance(ENGINE_VERSION, str)
    assert ENGINE_VERSION == "tickflow-signal-v1"
