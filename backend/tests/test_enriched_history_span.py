"""get_enriched_history_span 的区间判定测试。

该方法供自选 enriched 端点判定「加入日是否在可查询历史范围内」, 必须满足:
  - 纯读 O(1): 缓存冷/预热中不得触发 _refresh_enriched (端点随行情 tick 被反复调用);
  - 缓存不可用时返回 None, 而不是把「预热中」和「日期超窗」混为一谈。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.tickflow.repository import KlineRepository


def _bare_repo(cache=None, start=None, cache_date=None) -> KlineRepository:
    """跳过 __init__ (避免 DataStore/目录依赖), 只装配区间访问涉及的属性。"""
    repo = KlineRepository.__new__(KlineRepository)
    repo._enriched_history_cache = cache
    repo._enriched_history_start = start
    repo._enriched_cache_date = cache_date
    return repo


def _spy_refresh(repo):
    calls: list[int] = []
    repo._refresh_enriched = lambda: calls.append(1)  # type: ignore[method-assign]
    return calls


def test_span_none_when_cache_cold():
    repo = _bare_repo()
    calls = _spy_refresh(repo)

    assert repo.get_enriched_history_span() is None
    assert calls == [], "缓存冷时不得触发全量重算"


def test_span_none_when_cache_empty():
    repo = _bare_repo(cache=pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date}),
                      start=date(2026, 1, 1), cache_date=date(2026, 8, 14))

    assert repo.get_enriched_history_span() is None


def test_span_none_when_start_missing():
    """预热降级路径会写入 cache_date 但不装配历史起点, 此时不得报出区间。"""
    cache = pl.DataFrame({"symbol": ["600000.SH"], "date": [date(2026, 8, 14)]})
    repo = _bare_repo(cache=cache, start=None, cache_date=date(2026, 8, 14))

    assert repo.get_enriched_history_span() is None


def test_span_returns_bounds_without_refresh():
    cache = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "date": [date(2026, 1, 1), date(2026, 8, 14)],
    })
    repo = _bare_repo(cache=cache, start=date(2026, 1, 1), cache_date=date(2026, 8, 14))
    calls = _spy_refresh(repo)

    assert repo.get_enriched_history_span() == (date(2026, 1, 1), date(2026, 8, 14))
    assert calls == [], "命中缓存时必须纯读, 不得触发 _refresh_enriched"


def test_span_falls_back_to_column_max():
    """cache_date 缺失时回退取日期列最大值 (仍不触发刷新)。"""
    cache = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "date": [date(2026, 1, 1), date(2026, 8, 14)],
    })
    repo = _bare_repo(cache=cache, start=date(2026, 1, 1), cache_date=None)
    calls = _spy_refresh(repo)

    assert repo.get_enriched_history_span() == (date(2026, 1, 1), date(2026, 8, 14))
    assert calls == []
