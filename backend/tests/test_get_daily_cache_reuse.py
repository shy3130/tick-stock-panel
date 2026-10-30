"""get_daily 复用 enriched 历史缓存的等价性测试。

个股对话框打开时 /api/kline/daily 每个行情 tick 调用一次; 旧路径每次
150 天扫描 + 全套指标重算, 新路径优先从预计算历史缓存裁剪。
本测试证明: 同一份数据下两条路径输出逐列一致, 且缓存命中时不触发扫描。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from polars.testing import assert_frame_equal

from app.tickflow.repository import KlineRepository

SYM = "600001.SH"


def _raw_frame(days: int = 80) -> pl.DataFrame:
    """构造 ~57 个交易日的 14 列形态数据 (复权价与原始价一致, 无除权)。"""
    base = date(2026, 4, 1)
    rows = []
    price = 10.0
    d = base
    while len(rows) < days:
        if d.weekday() < 5:
            open_ = price * (1 + ((len(rows) % 7) - 3) * 0.004)
            close = price * (1 + ((len(rows) % 5) - 2) * 0.006)
            high = max(open_, close) * 1.01
            low = min(open_, close) * 0.99
            price = close
            rows.append({
                "symbol": SYM, "date": d,
                "open": round(open_, 4), "high": round(high, 4),
                "low": round(low, 4), "close": round(close, 4),
                "volume": 10000.0 + (len(rows) % 10) * 500.0,
                "amount": 1.0e7,
                "raw_close": round(close, 4), "raw_high": round(high, 4),
                "raw_low": round(low, 4),
            })
        d += timedelta(days=1)
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _bare_repo(raw: pl.DataFrame) -> tuple[KlineRepository, dict]:
    repo = KlineRepository.__new__(KlineRepository)
    repo._enriched_history_cache = None
    repo._enriched_history_start = None
    repo._enriched_cache = None
    repo._enriched_cache_date = None
    repo.get_instruments = lambda: pl.DataFrame()  # type: ignore[method-assign]
    repo.get_historical_shares = lambda: pl.DataFrame()  # type: ignore[method-assign]
    repo.get_enriched_latest = lambda **kwargs: (pl.DataFrame(), None)  # type: ignore[method-assign]
    calls = {"scan": 0}

    def _scan(symbol, start, end, columns):
        calls["scan"] += 1
        return raw.filter((pl.col("date") >= start) & (pl.col("date") <= end))

    repo._scan_daily_symbol = _scan  # type: ignore[method-assign]
    return repo, calls


def test_get_daily_cache_path_matches_scan_path():
    raw = _raw_frame()
    dates = raw["date"].to_list()
    start, end = dates[30], dates[-1]

    # 旧路径: 无历史缓存 → 扫描 + 即时计算
    repo_old, calls_old = _bare_repo(raw)
    result_old = repo_old.get_daily(SYM, start, end)
    assert calls_old["scan"] == 1

    # 新路径: 预计算历史缓存 (同一真实计算栈 _compute_enriched_range 构建)
    repo_new, calls_new = _bare_repo(raw)
    hist = repo_new._compute_enriched_range(raw)
    repo_new._enriched_history_cache = hist
    repo_new._enriched_history_start = hist["date"].min()
    result_new = repo_new.get_daily(SYM, start, end)

    assert calls_new["scan"] == 0, "缓存命中时不得回退到扫描路径"
    assert result_new.height == result_old.height

    common = sorted(set(result_old.columns) & set(result_new.columns))
    assert {"open", "close", "ma5", "ma20", "ma60", "rsi_14"} <= set(common)
    assert_frame_equal(
        result_old.sort("date").select(common),
        result_new.sort("date").select(common),
        check_exact=False,
        rel_tol=1e-9,
    )


def test_get_daily_falls_back_to_scan_when_cache_does_not_cover_start():
    raw = _raw_frame()
    dates = raw["date"].to_list()

    repo, calls = _bare_repo(raw)
    hist = repo._compute_enriched_range(raw)
    repo._enriched_history_cache = hist
    repo._enriched_history_start = hist["date"].min()

    # 请求起点早于缓存覆盖 → 必须回退扫描路径
    result = repo.get_daily(SYM, dates[0] - timedelta(days=5), dates[-1])
    assert calls["scan"] == 1
    assert not result.is_empty()
