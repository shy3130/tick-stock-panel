"""分钟K精确成交 (_resolve_minute_fill / _load_minute_for_fills) 回归测试。

背景: 原实现用 df.to_numpy() 转 structured array 再按字段名索引 (arr["open"])。
当 DataFrame 含 datetime 列 + float 列时, to_numpy() 退化为 dtype=object 的二维
数组, 字段名索引抛 IndexError: "only integers, slices... are valid indices"。
开启 minute_fill 的回测从未成功跑通过。

当前设计:
  - _load_minute_for_fills 按触发日分批读取分区 (get_minute_by_dates), 返回
    {(symbol, date_str): float64 2D ndarray} (列顺序 = _MINUTE_NUMERIC_COLS)。
  - _resolve_minute_fill 接收 float64 2D 数组, 按整数列索引访问。
.cast(Float64) 保证 to_numpy 不退化, 整数索引避免列名依赖。
"""
from __future__ import annotations

from datetime import date, datetime

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine

NUMERIC_COLS = BacktestEngine._MINUTE_NUMERIC_COLS  # open/high/low/close/volume/amount


def _sample_minute_df(symbol: str = "000001.SZ") -> pl.DataFrame:
    """构造一份带 datetime 列 + float 列的分钟K (get_minute_by_dates 的返回形态)。"""
    base = datetime(2024, 1, 2, 9, 31)
    return pl.DataFrame({
        "symbol": [symbol] * 4,
        "datetime": [base.replace(hour=h, minute=m) for h, m in
                     [(9, 31), (10, 0), (14, 0), (14, 57)]],
        "open": [10.0, 10.5, 10.8, 10.6],
        "high": [10.6, 10.7, 10.9, 10.7],
        "low": [9.9, 10.4, 10.7, 10.5],
        "close": [10.2, 10.6, 10.85, 10.65],
        "volume": [100, 200, 150, 120],
        "amount": [1020.0, 2120.0, 1627.0, 1278.0],
    })


def _to_compact_arr(mdf: pl.DataFrame) -> np.ndarray:
    """模拟 _load_minute_for_fills 的转换: 统一 float64 再 to_numpy。"""
    cols = [c for c in NUMERIC_COLS if c in mdf.columns]
    return mdf.select([pl.col(c).cast(pl.Float64) for c in cols]).to_numpy()


def test_resolve_minute_fill_with_compact_array_no_index_error():
    """紧凑 float64 数组 + 整数列索引不再抛 IndexError (锁定原 bug)。

    原始 bug: arr["open"] 在 object 数组上炸。现在 .cast(Float64) 保证类型一致,
    to_numpy 返回规整 2D float64, 整数索引 arr[:,0] 稳定。
    """
    arr = _to_compact_arr(_sample_minute_df())
    assert arr.dtype == np.float64  # 必须是 float64, 不能退化成 object
    # 三种分支都应正常返回
    assert BacktestEngine._resolve_minute_fill(arr, ref_price=10.5, side="buy") is not None
    assert BacktestEngine._resolve_minute_fill(arr, ref_price=10.5, side="sell") is not None
    vwap = BacktestEngine._resolve_minute_fill(arr, ref_price=None, side="buy")
    assert vwap is not None and vwap > 0


def test_resolve_minute_fill_buy_cross_above_ref():
    """买入: 价格涨破参考线 → 开盘已高于则按开盘。"""
    arr = _to_compact_arr(_sample_minute_df())
    assert BacktestEngine._resolve_minute_fill(arr, 9.5, "buy") == 10.0


def test_resolve_minute_fill_sell_cross_below_ref():
    """卖出: 价格跌破参考线 → 开盘已低于则按开盘。"""
    arr = _to_compact_arr(_sample_minute_df())
    assert BacktestEngine._resolve_minute_fill(arr, 10.5, "sell") == 10.0


def test_resolve_minute_fill_vwap():
    """无参考线 → VWAP = 总成交额 / 总成交量。"""
    arr = _to_compact_arr(_sample_minute_df())
    total_amt = 1020.0 + 2120.0 + 1627.0 + 1278.0
    total_vol = 100 + 200 + 150 + 120
    assert BacktestEngine._resolve_minute_fill(arr, None, "buy") == total_amt / total_vol


def test_resolve_minute_fill_empty_returns_none():
    """空数组 → None (降级到日K口径)。"""
    assert BacktestEngine._resolve_minute_fill(np.array([]).reshape(0, 6), None, "buy") is None
    assert BacktestEngine._resolve_minute_fill(None, None, "buy") is None


class _FakeRepo:
    """最小 repo 桩: get_minute_by_dates 直接返回预构造的混合列 DataFrame。"""

    def __init__(self, df: pl.DataFrame) -> None:
        self._df = df

    def get_minute_by_dates(self, symbols, dates, asset_type="stock"):  # noqa: ANN001
        return self._df


def test_load_minute_for_fills_returns_compact_arrays():
    """_load_minute_for_fills 返回 {(symbol, date_str): float64 ndarray}。

    锁定两个关键性质:
      1) cache 值类型必须是 float64 ndarray (不能是 object, 也不能是臃肿的 DataFrame)。
      2) load 时已做 .cast(Float64), _resolve_minute_fill 直接整数索引可用。
    """
    df = _sample_minute_df()
    repo = _FakeRepo(df)
    result = BacktestEngine._load_minute_for_fills(
        repo, ["000001.SZ"], {"2024-01-02"}, "stock",
    )
    assert ("000001.SZ", "2024-01-02") in result
    arr = result[("000001.SZ", "2024-01-02")]
    # 关键断言: 紧凑 float64 数组, 非 object, 非 DataFrame
    assert isinstance(arr, np.ndarray)
    assert arr.dtype == np.float64
    # 列顺序 = _MINUTE_NUMERIC_COLS (open=0, high=1, low=2, close=3, volume=4, amount=5)
    assert arr.shape == (4, 6)
    # 端到端: load → resolve 不抛异常
    price = BacktestEngine._resolve_minute_fill(arr, None, "buy")
    assert price is not None and price > 0


def test_load_minute_for_fills_handles_missing_dates():
    """缺失的日期分区不报错, 直接跳过。"""
    repo = _FakeRepo(pl.DataFrame())  # 空返回
    result = BacktestEngine._load_minute_for_fills(
        repo, ["000001.SZ"], {"2024-01-02", "2024-01-03"}, "stock",
    )
    assert result == {}
