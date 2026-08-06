"""港股 enriched 管道 —— repository 层落盘/读回/缓存测试。

compute_all 的 asset_type 分支已在 tests/indicators/test_pipeline_hk.py 覆盖,
这里只测本次新增的部分:append_hk_enriched 落全量列(原先只落 5 列的 bug)、
港股维表读写、enriched 内存缓存、以及不复权口径。
"""
from datetime import date

import polars as pl

from app.indicators.pipeline import ENRICHED_STORAGE_COLS, compute_enriched
from app.storage.repository import DataStore, KlineRepository


def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def _hk_daily(symbol: str = "00700.HK") -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol, symbol, symbol],
        "date": [date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1)],
        "open": [100.0, 100.0, 108.0],
        "high": [100.0, 110.0, 112.0],
        "low": [100.0, 100.0, 107.0],
        "close": [100.0, 110.0, 109.0],
        "volume": [1_000.0, 1_200.0, 900.0],
        "amount": [100_000.0, 110_000.0, 98_000.0],
    })


def _hk_instruments(symbol: str = "00700.HK") -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol],
        "name": ["腾讯控股"],
        "code": [symbol.split(".")[0]],
        "float_shares": [1_000_000.0],
        "total_shares": [1_000_000.0],
    })


def test_append_hk_enriched_writes_full_storage_columns(tmp_path):
    """回归测试:原先 append_hk_enriched 只落 symbol/date/close/change_pct/source
    五列,面板算不出任何指标。现在必须落 ENRICHED_STORAGE_COLS 全量(按 in df.columns
    过滤,港股天然没有的涨跌停派生列会被自动跳过,但 volume/amount/raw_close 等
    基础列必须都在)。
    """
    r = repo(tmp_path)
    enriched = compute_enriched(
        _hk_daily(), factors=None, instruments=_hk_instruments(), asset_type="hk",
    )

    r.append_hk_enriched(enriched)

    written = pl.read_parquet(tmp_path / "kline_hk_enriched" / "date=2026-07-01" / "part.parquet")
    # 至少要含 A 股/ETF 同款的基础行情列 —— 这是原 bug 缺失的核心列
    for col in ("open", "high", "low", "volume", "amount", "raw_close", "raw_high", "raw_low"):
        assert col in written.columns, f"港股 enriched 落盘缺少 {col} 列(面板将无法计算指标)"
    # 涨跌停派生列港股天然没有,不应该出现在存储列里
    assert "consecutive_limit_ups" not in written.columns


def test_hk_enriched_not_adjusted(tmp_path):
    """factors=None → 不复权,raw_close 与 close 相等(本地无港股除权数据源)。"""
    enriched = compute_enriched(
        _hk_daily(), factors=None, instruments=_hk_instruments(), asset_type="hk",
    )
    assert enriched["close"].to_list() == enriched["raw_close"].to_list()


def test_hk_instruments_roundtrip(tmp_path):
    """save_hk_instruments 落盘 → get_hk_instruments 读回,float_shares 保留
    (enriched 算换手率要用)。"""
    r = repo(tmp_path)
    r.save_hk_instruments(_hk_instruments())

    out = r.get_hk_instruments()

    assert out.height == 1
    assert out["symbol"].to_list() == ["00700.HK"]
    assert out["float_shares"].to_list() == [1_000_000.0]


def test_get_instruments_asset_dispatches_hk(tmp_path):
    """get_instruments_asset("hk") 走到港股维表,不是空 DataFrame 兜底。"""
    r = repo(tmp_path)
    r.save_hk_instruments(_hk_instruments())

    out = r.get_instruments_asset("hk")

    assert out.height == 1


def test_hk_enriched_latest_cache_via_asset_dispatch(tmp_path):
    """append_hk_enriched 写盘后,get_enriched_latest_asset("hk") 能读到最新日缓存
    (验证 _refresh_hk_enriched 被正确触发,而非一直返回空)。
    """
    r = repo(tmp_path)
    r.save_hk_instruments(_hk_instruments())
    enriched = compute_enriched(
        _hk_daily(), factors=None, instruments=_hk_instruments(), asset_type="hk",
    )
    r.append_hk_enriched(enriched)

    df, cache_date = r.get_enriched_latest_asset("hk")

    assert cache_date == date(2026, 7, 1)
    assert not df.is_empty()
    assert "ma5" in df.columns  # 内存缓存里应含即时计算的指标,而非仅存储列


def test_clear_cache_resets_hk_state(tmp_path):
    """clear_cache() 必须把港股缓存一并清空,否则清数据后港股面板仍显示旧数据。"""
    r = repo(tmp_path)
    r.save_hk_instruments(_hk_instruments())
    enriched = compute_enriched(
        _hk_daily(), factors=None, instruments=_hk_instruments(), asset_type="hk",
    )
    r.append_hk_enriched(enriched)
    r.get_enriched_latest_asset("hk")  # 触发缓存填充
    assert r._hk_enriched_cache is not None

    r.clear_cache()

    assert r._hk_enriched_cache is None
    assert r._hk_enriched_cache_date is None
    # 港股完整历史缓存已删除, 不应再存在该属性
    assert not hasattr(r, "_hk_enriched_history_cache")
    assert r._hk_instruments_cache is None
