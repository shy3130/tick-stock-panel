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
import pytest

from app.backtest.engine import BacktestEngine
from app.backtest.minute_trigger import build_minute_entry_reference, build_minute_exit_reference

NUMERIC_COLS = BacktestEngine._MINUTE_NUMERIC_COLS  # open/high/low/close/volume/amount


def _sample_minute_df(symbol: str = "000001.SZ") -> pl.DataFrame:
    """构造一份带 datetime 列 + float 列的分钟K (get_minute_by_dates 的返回形态)。

    volume/amount 按数据契约: volume 单位手, amount 单位元 = 价 x 手 x 100。
    """
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
        "amount": [10.2 * 100 * 100, 10.6 * 200 * 100, 10.85 * 150 * 100, 10.65 * 120 * 100],
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
    """无参考线 → VWAP = 总成交额 / (总成交量x100)。

    volume 单位手、amount 单位元, VWAP 必须除以股数 (x100) — 与 scoring/
    intraday_features/matrix 的 VWAP 同口径 (#387)。量级护栏拦住 100 倍
    量纲错误: 均价必须落在当日价格区间附近, 而不是夹具数值的巧合比值。
    """
    arr = _to_compact_arr(_sample_minute_df())
    total_amt = 10.2 * 100 * 100 + 10.6 * 200 * 100 + 10.85 * 150 * 100 + 10.65 * 120 * 100
    total_vol = (100 + 200 + 150 + 120) * 100  # 手 → 股
    vwap = BacktestEngine._resolve_minute_fill(arr, None, "buy")
    assert vwap == pytest.approx(total_amt / total_vol)
    assert 10.0 < vwap < 11.0  # 量级护栏: 真实均价量级, 拦截缺 x100 的 100 倍错误


def test_resolve_minute_fill_empty_returns_none():
    """空数组 → None (降级到日K口径)。"""
    assert BacktestEngine._resolve_minute_fill(np.array([]).reshape(0, 6), None, "buy") is None
    assert BacktestEngine._resolve_minute_fill(None, None, "buy") is None


def test_resolve_minute_exit_trigger_uses_next_minute_open():
    arr = np.array([
        [10.2, 10.3, 10.1, 10.2, 100, 1020],
        [10.1, 10.2, 9.8, 9.9, 100, 990],
        [9.7, 9.8, 9.6, 9.7, 100, 970],
    ], dtype=np.float64)

    assert BacktestEngine._resolve_minute_exit_trigger(arr, 10.0) == 9.7


def test_resolve_minute_exit_trigger_without_next_bar_returns_none():
    arr = np.array([
        [10.2, 10.3, 10.1, 10.2, 100, 1020],
        [10.1, 10.2, 9.8, 9.9, 100, 990],
    ], dtype=np.float64)

    assert BacktestEngine._resolve_minute_exit_trigger(arr, 10.0) is None


def test_minute_exit_reference_removes_current_close_from_ma20():
    close = np.array([[9.0]], dtype=np.float32)
    fields = {"ma20": np.array([[9.95]], dtype=np.float32)}
    codes = np.array([[0]], dtype=np.int16)

    result = build_minute_exit_reference(
        close,
        fields,
        codes,
        ("signal_ma20_breakdown",),
    )

    assert result[0, 0] == 10.0


def test_minute_entry_reference_removes_current_close():
    """买入参考线 = 前 4 日均值, 不随当日收盘变化 (#388)。

    rolling_mean(close,5) 含当根收盘 → 参考线依赖 15:00 才知道的收盘价 (前视)。
    修复后 (window·MA - close)/(window-1) 代数剔除当根, 当日开盘即已知,
    与卖出侧 build_minute_exit_reference 同一纪律。
    """
    base = [10.0, 10.0, 10.0, 10.0]
    refs = {}
    for c5 in (9.0, 9.6, 10.6, 11.0):
        close = np.array([*base, c5], dtype=np.float64).reshape(-1, 1)
        refs[c5] = float(build_minute_entry_reference(close)[4, 0])
    for c5, ref in refs.items():
        assert ref == pytest.approx(10.0), f"c5={c5}: 参考线不应随当日收盘变化"
    close = np.array([*base, 10.0], dtype=np.float64).reshape(-1, 1)
    result = build_minute_entry_reference(close)
    assert np.isnan(result[:4]).all()  # 不足窗口的行 → NaN → 退化 VWAP


def test_minute_fill_price_independent_of_current_close():
    """性质回归 (#388): 同一盘中路径只改当日收盘, 成交价必须相同。

    盘中下单那一刻不可能知道当日收盘; 若成交价随收盘漂移, 即为前视。
    """
    base = [10.0, 10.0, 10.0, 10.0]
    fills = {}
    for c5 in (10.6, 9.6):
        close = np.array([*base, c5], dtype=np.float64).reshape(-1, 1)
        ref = float(build_minute_entry_reference(close)[4, 0])
        # 同一盘中路径: 开盘 9.5 / 最高 10.6 / 最低 9.4, 只改最后一根分钟K收盘
        minute = np.array([
            [9.5, 10.6, 9.4, 9.5, 100, 9.5 * 100 * 100],
            [9.5, 10.6, 9.4, 9.5, 100, 9.5 * 100 * 100],
            [9.5, 10.6, 9.4, c5, 100, c5 * 100 * 100],
        ], dtype=np.float64)
        fills[c5] = BacktestEngine._resolve_minute_fill(minute, ref, "buy")
    assert fills[10.6] == pytest.approx(fills[9.6])
    assert 9.5 < fills[10.6] <= 10.6  # 成交价在当日已知的盘中范围内


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
