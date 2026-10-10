"""get_enriched_history 对历史日期也必须按「目标日及之前」的交易日计数取窗口。

策略页可以选历史日期 (DatePicker), run_all / 单策略运行都会以 as_of 调
_load_enriched_history(as_of, required_history_bars)。缓存命中路径
(repo.get_enriched_history) 从整份缓存的交易日序列取最后 N+1 个交易日作起点,
缓存里晚于 as_of 的交易日也被算进去: as_of 越早窗口越短, 早于 N 个交易日时
直接是空窗口。慢路径只读 date <= as_of, 本来就是按目标日计数的。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.services.screener import ScreenerService
from app.tickflow.repository import DataStore, KlineRepository

LOOKBACK = 20


def _trading_days(n: int = 220) -> list[date]:
    days: list[date] = []
    d = date(2025, 11, 3)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _cache(days: list[date]) -> pl.DataFrame:
    rows = []
    for symbol, phase in (("600001.SH", 0.0), ("000002.SZ", 1.0)):
        for i, day in enumerate(days):
            rows.append((symbol, day, round(10 + math.sin(i * 0.2 + phase), 2)))
    return pl.DataFrame(rows, schema=["symbol", "date", "close"], orient="row").sort(["symbol", "date"])


def _repo(tmp_path: Path, cache: pl.DataFrame) -> KlineRepository:
    repo = KlineRepository(DataStore(tmp_path))
    repo._enriched_history_cache = cache
    return repo


@pytest.mark.parametrize("back", [0, 1, 5, 15], ids=["latest", "back1", "back5", "back15"])
def test_history_window_counts_trading_days_up_to_target(tmp_path, back):
    days = _trading_days()
    target = days[-1 - back]
    out = _repo(tmp_path, _cache(days)).get_enriched_history(target, LOOKBACK)

    assert out is not None
    want = [d for d in days if d <= target][-(LOOKBACK + 1):]
    assert out["date"].unique().sort().to_list() == want


def test_screener_history_does_not_depend_on_later_cached_days(tmp_path):
    """同一个 as_of, 缓存是否已包含更晚的交易日不应改变策略拿到的历史窗口。"""
    days = _trading_days()
    as_of = days[-6]
    full = _cache(days)
    upto = full.filter(pl.col("date") <= as_of)

    with_later = ScreenerService(_repo(tmp_path / "a", full))._load_enriched_history(as_of, LOOKBACK)
    without_later = ScreenerService(_repo(tmp_path / "b", upto))._load_enriched_history(as_of, LOOKBACK)

    assert without_later["date"].n_unique() == LOOKBACK + 1
    assert with_later["date"].n_unique() == without_later["date"].n_unique()
    assert with_later.sort(["symbol", "date"]).equals(without_later.sort(["symbol", "date"]))
