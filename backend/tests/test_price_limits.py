from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

from app.api import kline
from app.backtest.matrix import load_market_data_matrix_from_parquet
from app.indicators import pipeline
from app.price_limits import (
    numpy_limit_price,
    numpy_price_limit_matrix,
    polars_is_risk_warning_name,
    polars_limit_price,
    polars_price_limit_pct,
    price_limit_pct,
)


@pytest.mark.parametrize(
    ("symbol", "trade_date", "is_st", "expected"),
    [
        ("600001.SH", date(2026, 7, 3), True, 0.05),
        ("600001.SH", date(2026, 7, 6), True, 0.10),
        ("000001.SZ", date(2026, 7, 3), False, 0.10),
        ("300001.SZ", date(2026, 7, 3), True, 0.20),
        ("688001.SH", date(2026, 7, 3), True, 0.20),
        ("689001.SH", date(2026, 7, 3), True, 0.20),
        ("830001.BJ", date(2026, 7, 3), True, 0.30),
    ],
)
def test_scalar_price_limit_rules(symbol, trade_date, is_st, expected):
    assert price_limit_pct(
        symbol,
        trade_date,
        is_risk_warning=is_st,
    ) == pytest.approx(expected)


def test_polars_and_numpy_price_limit_rules_match():
    dates = [date(2026, 7, 3), date(2026, 7, 6)]
    symbols = ["600001.SH", "300001.SZ", "689001.SH", "830001.BJ"]
    names = ["*st主板", "*ST创业", "科创ST", "北交ST"]
    panel = pl.DataFrame({
        "date": [value for value in dates for _ in symbols],
        "symbol": symbols * len(dates),
        "name": names * len(dates),
    }).with_columns(
        polars_is_risk_warning_name(pl.col("name")).alias("is_st")
    ).with_columns(
        polars_price_limit_pct(
            pl.col("symbol"), pl.col("date"), pl.col("is_st"),
        ).alias("limit_pct")
    )
    polars_values = panel["limit_pct"].to_numpy().reshape(len(dates), len(symbols))
    numpy_values = numpy_price_limit_matrix(dates, symbols, names)
    np.testing.assert_allclose(polars_values, numpy_values)


def test_polars_and_numpy_limit_prices_use_identical_half_up_rounding():
    previous = np.array([18.90, 10.00], dtype=np.float64)
    limits = np.array([0.05, 0.10], dtype=np.float64)
    frame = pl.DataFrame({"previous": previous, "limit": limits})

    for up in (True, False):
        polars_values = frame.select(
            polars_limit_price(
                pl.col("previous"), pl.col("limit"), up=up,
            ).alias("price")
        )["price"].to_numpy()
        numpy_values = numpy_limit_price(previous, limits, up=up)
        np.testing.assert_allclose(polars_values, numpy_values)
    assert numpy_limit_price(previous, limits, up=False)[0] == pytest.approx(17.96)


def test_matrix_uses_date_specific_st_limits_across_change(tmp_path):
    root = tmp_path / "market"
    rows = [
        (date(2026, 7, 2), 10.0),
        (date(2026, 7, 3), 10.5),
        (date(2026, 7, 6), 11.03),
    ]
    for trade_date, close in rows:
        partition = root / f"date={trade_date.isoformat()}"
        partition.mkdir(parents=True)
        pl.DataFrame({
            "symbol": ["600001.SH"],
            "date": [trade_date],
            "open": [close],
            "high": [close],
            "low": [close],
            "close": [close],
            "raw_close": [close],
            "volume": [1000.0],
        }).write_parquet(partition / "part.parquet")

    market = load_market_data_matrix_from_parquet(
        root,
        rows[0][0],
        rows[-1][0],
        field_columns={"raw_close", "price_limit_pct"},
        instruments=pl.DataFrame({
            "symbol": ["600001.SH"],
            "name": ["*ST主板"],
        }),
        cache_root=tmp_path / "cache",
    )
    np.testing.assert_allclose(
        market.field("price_limit_pct")[:, 0],
        np.array([0.05, 0.05, 0.10], dtype=np.float32),
    )
    assert market.limit_up_locked[:, 0].tolist() == [0, 1, 0]


class _InstrumentRepo:
    def get_instruments_asset(self, asset_type: str) -> pl.DataFrame:
        assert asset_type == "stock"
        return pl.DataFrame({
            "symbol": ["600001.SH"],
            "limit_up": [10.88],
            "limit_down": [8.90],
        })


def test_minute_price_limit_prefers_authoritative_prices_only_today(monkeypatch):
    today = date(2026, 7, 18)
    monkeypatch.setattr(kline, "cn_today", lambda: today)
    current = kline._get_price_limit_info(
        _InstrumentRepo(), "600001.SH", today, "stock", "*ST主板",
    )
    historical = kline._get_price_limit_info(
        _InstrumentRepo(), "600001.SH", date(2026, 7, 3), "stock", "*ST主板",
    )

    assert current == {
        "rate": 0.10,
        "limit_up": 10.88,
        "limit_down": 8.90,
        "no_limit": False,
        "source": "instrument",
    }
    assert historical == {
        "rate": 0.05,
        "limit_up": None,
        "limit_down": None,
        "no_limit": False,
        "source": "rule",
    }


class _NewStockRepo:
    """listing_date 在无涨跌幅窗口内的注册制新股维表 (C沈鼓场景)。"""

    def __init__(self, listing: date):
        self.listing = listing

    def get_instruments_asset(self, asset_type: str) -> pl.DataFrame:
        assert asset_type == "stock"
        return pl.DataFrame({
            "symbol": ["601091.SH"],
            "name": ["C沈鼓"],
            "limit_up": [100000.0],   # 哨兵值
            "limit_down": [None],
            "listing_date": [self.listing],
        })


def test_minute_price_limit_no_limit_window_overrides_rate_and_sentinel():
    """listing_date 命中窗口: 历史日 (哨兵/as_of 均失效) 也返回 no_limit=True。"""
    listing = date(2026, 9, 17)
    repo = _NewStockRepo(listing)

    # 行情日 = 上市次日 (窗口内), 维表 as_of 与行情日无关 (无 as_of 列)
    info = kline._get_price_limit_info(repo, "601091.SH", date(2026, 9, 18), "stock", "C沈鼓")

    assert info is not None
    assert info["no_limit"] is True
    assert info["limit_up"] is None
    assert info["limit_down"] is None

    # 窗口外 (第 6 个交易日之后) 恢复 rate 口径
    after = kline._get_price_limit_info(repo, "601091.SH", date(2026, 10, 15), "stock", "沈鼓能源")
    assert after is not None
    assert after["no_limit"] is False
    assert after["rate"] == 0.10


def _daily_limit_rows(current_close: float) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600001.SH", "600001.SH"],
        "date": [date(2026, 7, 17), date(2026, 7, 20)],
        "open": [10.0, current_close],
        "high": [10.0, current_close],
        "low": [10.0, current_close],
        "close": [10.0, current_close],
        "raw_close": [10.0, current_close],
        "raw_high": [10.0, current_close],
    })


@pytest.mark.parametrize(
    ("instrument_as_of", "expected"),
    [
        (date(2026, 7, 17), False),
        (date(2026, 7, 20), True),
        (None, True),
    ],
)
def test_daily_limit_prices_require_matching_instrument_date(instrument_as_of, expected):
    instrument_data = {
        "symbol": ["600001.SH"],
        "name": ["普通股"],
        "limit_up": [10.90],
        "limit_down": [9.10],
    }
    if instrument_as_of is not None:
        instrument_data["as_of"] = [instrument_as_of]

    result = pipeline.compute_limit_signals(
        _daily_limit_rows(9.10),
        pl.DataFrame(instrument_data),
        needed={"signal_limit_down"},
    )

    assert result["signal_limit_down"][-1] is expected
    assert "_instrument_as_of" not in result.columns


def test_daily_limit_prices_ignore_zero_placeholder_and_match_realtime():
    """维表涨跌停价为 0 (数据源未提供该字段的占位值) 时必须回退理论价。

    直接采用 0 会让「raw_close >= 0 - 0.005」恒成立, 当日所有标的被判涨停,
    连板数一路累加; 跌停侧反过来永远判不出跌停。实时路径
    (_compute_limit_signals_today) 已有 >0 守卫, 冷路径必须同口径。
    """
    instruments = pl.DataFrame({
        "symbol": ["600001.SH"],
        "name": ["普通股"],
        "limit_up": [0.0],
        "limit_down": [0.0],
        "as_of": [date(2026, 7, 20)],
    })

    # 只涨 0.5%: 不是涨停
    mild = pipeline.compute_limit_signals(
        _daily_limit_rows(10.05),
        instruments,
        needed={"signal_limit_up", "consecutive_limit_ups"},
    )
    assert mild["signal_limit_up"][-1] is False
    assert mild["consecutive_limit_ups"][-1] == 0

    # 真涨停 11.00 = 10.00 x 1.1: 理论价兜底后仍须判出
    sealed = pipeline.compute_limit_signals(
        _daily_limit_rows(11.00),
        instruments,
        needed={"signal_limit_up", "consecutive_limit_ups"},
    )
    assert sealed["signal_limit_up"][-1] is True
    assert sealed["consecutive_limit_ups"][-1] == 1

    # 真跌停 9.00 = 10.00 x 0.9: 占位 0 不得让跌停漏判
    floored = pipeline.compute_limit_signals(
        _daily_limit_rows(9.00),
        instruments,
        needed={"signal_limit_down"},
    )
    assert floored["signal_limit_down"][-1] is True

    # 与实时路径同一份维表同一结论
    realtime = pipeline._compute_limit_signals_today(
        pl.DataFrame({
            "symbol": ["600001.SH"],
            "date": [date(2026, 7, 20)],
            "open": [10.05],
            "high": [10.05],
            "low": [10.05],
            "close": [10.05],
            "raw_close": [10.05],
            "raw_high": [10.05],
            "raw_low": [10.05],
            "_prev_close_raw": [10.0],
            "volume": [1000.0],
        }),
        instruments,
    )
    assert realtime["signal_limit_up"][0] is False
    assert mild["signal_limit_up"][-1] is realtime["signal_limit_up"][0]


def test_realtime_limit_prices_ignore_stale_instrument_date():
    today = date(2026, 7, 20)
    rows = pl.DataFrame({
        "symbol": ["600001.SH"],
        "date": [today],
        "open": [9.10],
        "high": [9.10],
        "low": [9.10],
        "close": [9.10],
        "raw_close": [9.10],
        "raw_high": [9.10],
        "raw_low": [9.10],
        "_prev_close_raw": [10.0],
        "volume": [1000.0],
    })
    instruments = pl.DataFrame({
        "symbol": ["600001.SH"],
        "name": ["普通股"],
        "limit_up": [10.90],
        "limit_down": [9.10],
        "as_of": [date(2026, 7, 17)],
    })

    result = pipeline._compute_limit_signals_today(rows, instruments)

    assert result["signal_limit_down"][0] is False
    assert "_instrument_as_of" not in result.columns


def test_limit_down_recovery_uses_raw_low_under_later_ex_div():
    """除权事件之后重算历史时, 跌停翘板"曾触及跌停"必须用原始价 low 判断。

    day2 (历史日): 原始 low 9.30 未触及跌停价 9.00, 不应触发翘板;
    但 day3 除权 (ex_factor=2) 使 day2 前复权 low 变为 4.65,
    若误用复权 low 对比原始口径跌停价会误报翘板。
    day3 (除权日, 最新日不复权): 涨跌停基准切换为前复权昨收 4.825 → 跌停价 4.34,
    原始 low 4.34 触及且收阳未封死 → 真翘板。
    """
    raw = pl.DataFrame({
        "symbol": ["600001.SH"] * 3,
        "date": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        "open": [10.00, 9.60, 4.30],
        "high": [10.10, 9.70, 4.45],
        "low": [9.90, 9.30, 4.34],
        "close": [10.00, 9.65, 4.42],
        "volume": [10000.0, 10000.0, 10000.0],
        "amount": [1.0e7, 1.0e7, 1.0e7],
    })
    factors = pl.DataFrame({
        "symbol": ["600001.SH"],
        "trade_date": [date(2024, 1, 4)],
        "ex_factor": [2.0],
    })
    instruments = pl.DataFrame({
        "symbol": ["600001.SH"],
        "name": ["普通股"],
        "float_shares": [1.0e8],
    })

    df = pipeline.compute_enriched(raw, factors=factors, instruments=instruments)

    day2 = df.filter(pl.col("date") == date(2024, 1, 3))
    assert day2["signal_limit_down_recovery"][0] is False
    day3 = df.filter(pl.col("date") == date(2024, 1, 4))
    assert day3["signal_limit_down_recovery"][0] is True
