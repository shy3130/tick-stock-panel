"""港股 /api/data/status 聚合 —— 用真实 DuckDB 连接跑,不用 stub。

回归测试:instruments_hk 这张 DuckDB 视图曾经从未被注册(_register_views /
refresh_index_views 只抄了 instruments_index/instruments_etf,漏了 instruments_hk)。
_safe_aggregate_hk_instruments 对着不存在的视图查询会抛异常,被外层
`except Exception` 静默吞掉后返回 None —— 用手写 execute_one stub 的测试
测不出这类 bug,必须对真实 DuckDB 连接跑一遍 SQL 才能抓住。
"""
from datetime import date

import polars as pl

from app.api.data import (
    _safe_aggregate_hk_daily,
    _safe_aggregate_hk_enriched,
    _safe_aggregate_hk_instruments,
)
from app.indicators.pipeline import compute_enriched
from app.storage.repository import DataStore, KlineRepository


def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def _hk_daily() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["00700.HK", "00700.HK"],
        "date": [date(2026, 6, 30), date(2026, 7, 1)],
        "open": [100.0, 108.0],
        "high": [110.0, 112.0],
        "low": [100.0, 107.0],
        "close": [110.0, 109.0],
        "volume": [1_200.0, 900.0],
        "amount": [110_000.0, 98_000.0],
    })


def _hk_instruments() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["00700.HK"],
        "name": ["腾讯控股"],
        "code": ["00700"],
        "float_shares": [1_000_000.0],
    })


def test_hk_instruments_status_reads_real_view(tmp_path):
    """save_hk_instruments 落盘后,_safe_aggregate_hk_instruments 必须能通过
    真实 DuckDB 视图查到数据 —— 而不是因视图未注册而静默返回 None。

    DuckDB 的 CREATE VIEW ... FROM read_parquet(glob) 在建视图时就要求 glob
    至少命中一个文件(不是惰性求值),所以 DataStore 构造时 instruments_hk/
    还是空目录、视图建不出来是正常的;真实管道(sync_hk_instruments)写完
    parquet 后会调 refresh_index_views() 重建视图,这里照抄同一顺序。
    """
    r = repo(tmp_path)
    r.save_hk_instruments(_hk_instruments())
    r.refresh_index_views()

    stats = _safe_aggregate_hk_instruments(r)

    assert stats is not None, "instruments_hk 视图查询失败(很可能是视图未注册)"
    assert stats["rows"] == 1
    assert stats["symbols_covered"] == 1
    assert stats["named"] == 1


def test_hk_daily_status_reads_real_view(tmp_path):
    r = repo(tmp_path)
    r.append_hk_daily(_hk_daily())
    r.refresh_index_views()

    stats = _safe_aggregate_hk_daily(r)

    assert stats is not None
    assert stats["rows"] == 2
    assert stats["earliest_date"] == "2026-06-30"
    assert stats["latest_date"] == "2026-07-01"
    assert stats["symbols_covered"] == 1


def test_hk_enriched_status_reads_real_view(tmp_path):
    r = repo(tmp_path)
    enriched = compute_enriched(
        _hk_daily(), factors=None, instruments=_hk_instruments(), asset_type="hk",
    )
    r.append_hk_enriched(enriched)
    r.refresh_index_views()

    stats = _safe_aggregate_hk_enriched(r)

    assert stats is not None
    assert stats["rows"] == 2
    assert stats["fields"] > 0
