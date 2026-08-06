"""PanelCache 回归测试: LRU + TTL + 字节淘汰 + 超大绕过 + invalidate。

防止两个超大回测面板重现「全市场历史长期常驻」的原留存模式。
通过 monkeypatch _estimate_panel_bytes 控制字节, 避免分配数百 MiB。
"""
from __future__ import annotations

import time
from datetime import date

import polars as pl

from app.backtest import engine


def _mkdf(tag: str = "x", n: int = 3) -> pl.DataFrame:
    return pl.DataFrame({"symbol": [tag] * n, "date": [date(2026, 1, 1)] * n, "v": list(range(n))})


def _cache(monkeypatch, max_bytes=512 * 1024 * 1024):
    monkeypatch.setattr(engine, "_estimate_panel_bytes", lambda df: int(df.estimated_size()))
    return engine.PanelCache(max_size=2, ttl_seconds=180, max_bytes=max_bytes)


def _slow_compute(frames):
    """返回一个 compute_fn, 每次 miss 计数 +1 并返回对应帧。"""
    calls = {"n": 0}

    def _fn(symbols, start, end, columns):
        idx = calls["n"]
        calls["n"] += 1
        return frames[min(idx, len(frames) - 1)]

    return _fn, calls


def test_cache_hit_avoids_recompute(monkeypatch):
    pc = _cache(monkeypatch)
    frames = [_mkdf("a")]
    fn, calls = _slow_compute(frames)
    d1 = date(2026, 1, 1)
    r1 = pc.get_or_compute(None, d1, d1, None, fn)
    r2 = pc.get_or_compute(None, d1, d1, None, fn)
    assert calls["n"] == 1, "第二次应命中缓存不重算"
    assert r1.equals(r2)
    pc.invalidate()


def test_lru_evicts_oldest_when_exceeding_max_size(monkeypatch):
    pc = _cache(monkeypatch)
    fn, calls = _slow_compute([_mkdf(str(i)) for i in range(3)])
    d = lambda i: date(2026, 1, 1 + i)  # noqa: E731
    # 三个不同 key, max_size=2 → 第一个被淘汰
    pc.get_or_compute(None, d(0), d(0), None, fn)
    pc.get_or_compute(None, d(1), d(1), None, fn)
    pc.get_or_compute(None, d(2), d(2), None, fn)
    assert len(pc._cache) == 2
    # d0 已淘汰
    fn2, calls2 = _slow_compute([_mkdf("fresh")])
    r = pc.get_or_compute(None, d(0), d(0), None, fn2)
    assert calls2["n"] == 1, "d0 miss (已被 LRU 淘汰, 需重算)"
    pc.invalidate()


def test_ttl_expiry_evicts_entry(monkeypatch):
    pc = engine.PanelCache(max_size=2, ttl_seconds=1, max_bytes=512 * 1024 * 1024)
    monkeypatch.setattr(engine, "_estimate_panel_bytes", lambda df: int(df.estimated_size()))
    fn, calls = _slow_compute([_mkdf("a"), _mkdf("b")])
    d = date(2026, 1, 1)
    pc.get_or_compute(None, d, d, None, fn)
    # 等 TTL 过期 (ttl=1s)
    time.sleep(1.2)
    r = pc.get_or_compute(None, d, d, None, fn)
    assert calls["n"] == 2, "TTL 过期后应重算"
    pc.invalidate()


def test_byte_eviction_keeps_total_under_budget(monkeypatch):
    # 每帧 0.6 * budget, 两帧即超 budget → 写第二帧淘汰第一帧
    budget = 1000
    monkeypatch.setattr(engine, "_estimate_panel_bytes", lambda df: int(budget * 0.6))
    pc = engine.PanelCache(max_size=10, ttl_seconds=180, max_bytes=budget)
    fn, calls = _slow_compute([_mkdf("a"), _mkdf("b")])
    d = lambda i: date(2026, 1, 1 + i)  # noqa: E731
    pc.get_or_compute(None, d(0), d(0), None, fn)
    pc.get_or_compute(None, d(1), d(1), None, fn)
    total = sum(e.size for e in pc._cache.values())
    assert total <= budget, "累计字节不超过预算"
    # 最早帧 a 被淘汰
    fn2, calls2 = _slow_compute([_mkdf("fresh")])
    pc.get_or_compute(None, d(0), d(0), None, fn2)
    assert calls2["n"] == 1, "a 被字节淘汰, 需重算"
    pc.invalidate()


def test_oversized_frame_returned_but_not_cached(monkeypatch):
    budget = 1000
    monkeypatch.setattr(engine, "_estimate_panel_bytes", lambda df: budget + 1)
    pc = engine.PanelCache(max_size=2, ttl_seconds=180, max_bytes=budget)
    fn, calls = _slow_compute([_mkdf("big")])
    d = date(2026, 1, 1)
    r = pc.get_or_compute(None, d, d, None, fn)
    assert not r.is_empty(), "超大帧应正常返回"
    assert len(pc._cache) == 0, "超大帧不应入缓存"
    pc.invalidate()


def test_empty_frame_not_cached(monkeypatch):
    pc = _cache(monkeypatch)
    fn, _calls = _slow_compute([pl.DataFrame()])
    d = date(2026, 1, 1)
    r = pc.get_or_compute(None, d, d, None, fn)
    assert r.is_empty()
    assert len(pc._cache) == 0
    pc.invalidate()


def test_invalidate_drops_all(monkeypatch):
    pc = _cache(monkeypatch)
    fn, _calls = _slow_compute([_mkdf("a"), _mkdf("b")])
    d = lambda i: date(2026, 1, 1 + i)  # noqa: E731
    pc.get_or_compute(None, d(0), d(0), None, fn)
    pc.get_or_compute(None, d(1), d(1), None, fn)
    assert len(pc._cache) == 2
    pc.invalidate()
    assert len(pc._cache) == 0
