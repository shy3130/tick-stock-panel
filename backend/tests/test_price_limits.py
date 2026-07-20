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
        "source": "instrument",
    }
    assert historical == {
        "rate": 0.05,
        "limit_up": None,
        "limit_down": None,
        "source": "rule",
    }


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
