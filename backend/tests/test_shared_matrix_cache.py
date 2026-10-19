"""run_all 共享矩阵的进程级复用 (engine.build_shared_matrix 缓存)。

矩阵 = 纯函数(history 窗口, 字段并集), 数组构建后只读 — 同指纹直接复用免重建
(全市场矩阵构建数秒)。指纹含 enriched generation + 窗口范围, 数据/窗口变化
自动失效; generation 未知 (分钟/ETF/测试数据) 时 fail-closed 不缓存。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

from app.strategy.engine import StrategyDataContext, StrategyDef, StrategyEngine


class _FakeMatrixStrategy:
    """最小 matrix_strategy: 只暴露字段依赖接口。"""

    def __init__(self, fields: set[str]) -> None:
        self._fields = fields

    def required_fields(self) -> set[str]:
        return set(self._fields)

    def required_fields_for_params(self, params: dict | None = None) -> set[str]:
        return set()


def _matrix_def(sid: str, fields: set[str]) -> StrategyDef:
    return StrategyDef(
        meta={"id": sid, "name": sid, "timeframes": ["1d"], "asset_types": ["stock"]},
        basic_filter={},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        filter_fn=None,
        filter_history_fn=None,
        lookback_days=5,
        source="custom",
        execution_backend="matrix_native",
        matrix_strategy=_FakeMatrixStrategy(fields),
    )


def _history(days: int = 6) -> pl.DataFrame:
    syms = ["600000.SH", "000001.SZ"]
    rows = []
    for d in range(days):
        for s in syms:
            rows.append({
                "symbol": s, "name": s, "date": date(2026, 3, 2 + d),
                "open": 10.0, "high": 11.0, "low": 9.0,
                "close": 10.0 + d, "volume": 1000.0 + d,
            })
    return pl.DataFrame(rows)


def _engine_with(sid: str, fields: set[str]) -> StrategyEngine:
    engine = StrategyEngine(strategy_dirs=[Path("/nonexistent")])
    engine._strategies[sid] = _matrix_def(sid, fields)
    return engine


def _ctx(history: pl.DataFrame, generation: str | None) -> StrategyDataContext:
    return StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=date(2026, 3, 7),
        current=None, history=history, data_generation=generation,
    )


def test_same_fingerprint_returns_cached_matrix():
    engine = _engine_with("m1", {"close", "volume"})
    hist = _history()
    m1 = engine.build_shared_matrix(_ctx(hist, "g1"), [("m1", engine.get("m1"))])
    m2 = engine.build_shared_matrix(_ctx(hist, "g1"), [("m1", engine.get("m1"))])
    assert m1 is not None and m2 is not None
    assert m1 is m2, "同指纹应命中缓存返回同一对象"
    assert engine._shared_matrix_cache, "缓存应有一条记录"


def test_cached_matrix_equals_freshly_built():
    """对拍: 缓存命中对象与绕过缓存现建的对象逐元素相等 (纯优化承诺)。"""
    engine = _engine_with("m1", {"close", "volume"})
    hist = _history()
    cached = engine.build_shared_matrix(_ctx(hist, "g1"), [("m1", engine.get("m1"))])
    with engine._realtime_matrix_lock:
        engine._shared_matrix_cache.clear()
    fresh = engine.build_shared_matrix(_ctx(hist, "g1"), [("m1", engine.get("m1"))])
    assert cached is not fresh
    np.testing.assert_array_equal(cached.close, fresh.close)
    np.testing.assert_array_equal(cached.volume, fresh.volume)
    assert cached.symbols == fresh.symbols
    assert cached.timestamp_labels == fresh.timestamp_labels


def test_field_union_change_rebuilds():
    engine = _engine_with("m1", {"close"})
    engine._strategies["m2"] = _matrix_def("m2", {"momentum_20d"})
    hist = _history()
    small = engine.build_shared_matrix(
        _ctx(hist, "g1"), [("m1", engine.get("m1"))]
    )
    union = engine.build_shared_matrix(
        _ctx(hist, "g1"),
        [("m1", engine.get("m1")), ("m2", engine.get("m2"))],
    )
    assert small is not union, "字段并集变化必须重建"
    assert "momentum_20d" not in small.fields
    # 回到小子集: 指纹不同于并集 → 又一次重建 (缓存只保留最近一份)
    again = engine.build_shared_matrix(_ctx(hist, "g1"), [("m1", engine.get("m1"))])
    assert again is not union


def test_generation_change_rebuilds():
    engine = _engine_with("m1", {"close"})
    hist = _history()
    m1 = engine.build_shared_matrix(_ctx(hist, "g1"), [("m1", engine.get("m1"))])
    m2 = engine.build_shared_matrix(_ctx(hist, "g2"), [("m1", engine.get("m1"))])
    assert m1 is not m2, "enriched 换代后旧矩阵必须失效"


def test_window_change_rebuilds():
    """不同 lookback 的 history 窗口不可混用同一矩阵。"""
    engine = _engine_with("m1", {"close"})
    m1 = engine.build_shared_matrix(_ctx(_history(6), "g1"), [("m1", engine.get("m1"))])
    m2 = engine.build_shared_matrix(_ctx(_history(4), "g1"), [("m1", engine.get("m1"))])
    assert m1 is not m2
    assert len(m1.timestamp_labels) == 6
    assert len(m2.timestamp_labels) == 4


def test_no_generation_disables_cache():
    """generation 未知 (分钟/ETF/测试): fail-closed 不缓存, 保持旧行为。"""
    engine = _engine_with("m1", {"close"})
    hist = _history()
    m1 = engine.build_shared_matrix(_ctx(hist, None), [("m1", engine.get("m1"))])
    m2 = engine.build_shared_matrix(_ctx(hist, None), [("m1", engine.get("m1"))])
    assert m1 is not None and m2 is not None
    assert m1 is not m2
    assert not engine._shared_matrix_cache


def test_remove_strategy_clears_cache():
    engine = _engine_with("m1", {"close"})
    hist = _history()
    engine.build_shared_matrix(_ctx(hist, "g1"), [("m1", engine.get("m1"))])
    assert engine._shared_matrix_cache
    engine.unregister("m1")
    assert not engine._shared_matrix_cache, "删除策略应清空共享矩阵缓存"
