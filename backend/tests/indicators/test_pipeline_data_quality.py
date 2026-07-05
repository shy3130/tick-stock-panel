from datetime import date

import polars as pl

from app.data_providers.fquant_provider import FQuantProvider
from app.indicators.pipeline import compute_enriched, compute_enriched_today, compute_indicators, filter_halt_days


def test_turnover_rate_uses_share_volume_contract():
    raw = pl.DataFrame({
        "symbol": ["600519.SH"],
        "date": [date(2026, 7, 1)],
        "open": [10.0],
        "high": [11.0],
        "low": [9.0],
        "close": [10.5],
        "volume": [1_000_000.0],
        "amount": [10_000_000.0],
    })
    instruments = pl.DataFrame({
        "symbol": ["600519.SH"],
        "name": ["贵州茅台"],
        "float_shares": [100_000_000.0],
    })

    out = compute_enriched(raw, instruments=instruments)

    assert out["turnover_rate"].item() == 1.0


def test_filter_halt_days_drops_non_positive_ohlc():
    df = pl.DataFrame({
        "symbol": ["A", "B"],
        "open": [10.0, -1.0],
        "high": [11.0, 2.0],
        "low": [9.0, -2.0],
        "close": [10.5, -1.5],
    })

    out = filter_halt_days(df)

    assert out["symbol"].to_list() == ["A"]


def test_daily_change_uses_raw_prices_when_adjusted_close_spikes():
    df = pl.DataFrame({
        "symbol": ["000425.SZ"] * 3,
        "date": [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)],
        "open": [8.1, 250.0, 8.4],
        "high": [8.2, 253.0, 8.5],
        "low": [8.0, 240.0, 8.3],
        "close": [8.13, 253.38, 8.42],
        "raw_close": [8.13, 8.40, 8.42],
        "raw_high": [8.24, 8.49, 8.53],
        "raw_low": [7.80, 8.02, 8.31],
        "volume": [1_000_000.0, 1_100_000.0, 1_200_000.0],
        "amount": [8_000_000.0, 9_000_000.0, 10_000_000.0],
    })

    out = compute_indicators(df)
    row = out.filter(pl.col("date") == date(2026, 7, 2)).to_dicts()[0]

    assert abs(row["change_pct"] - (8.40 / 8.13 - 1)) < 1e-12
    assert abs(row["change_amount"] - 0.27) < 1e-12
    assert abs(row["amplitude"] - ((8.49 - 8.02) / 8.13)) < 1e-12


def test_enriched_today_recomputes_quote_change_after_adjustment():
    live_agg = pl.DataFrame({
        "symbol": ["600988.SH"],
        "ema5": [20.0],
        "ema10": [20.0],
        "ema20": [20.0],
        "ema30": [20.0],
        "ema60": [20.0],
        "_ema12": [20.0],
        "_ema26": [20.0],
        "macd_dea": [0.0],
        "_ma5_partial_sum": [80.0],
        "_ma10_partial_sum": [180.0],
        "_ma20_partial_sum": [380.0],
        "_ma30_partial_sum": [580.0],
        "_ma60_partial_sum": [1180.0],
        "_boll_partial_sum": [380.0],
        "_boll_partial_sq_sum": [7220.0],
        "_kdj_8d_low": [18.0],
        "_kdj_8d_high": [24.0],
        "kdj_k": [50.0],
        "kdj_d": [50.0],
        "atr_14": [1.0],
        "_rsi_avg_gain_6": [0.1],
        "_rsi_avg_loss_6": [0.1],
        "_rsi_avg_gain_14": [0.1],
        "_rsi_avg_loss_14": [0.1],
        "_rsi_avg_gain_24": [0.1],
        "_rsi_avg_loss_24": [0.1],
        "_vol_ma5_partial_sum": [4000.0],
        "_vol_ma10_partial_sum": [9000.0],
        "_high_59d": [24.0],
        "_low_59d": [18.0],
        "_close_5d_ago": [20.0],
        "_close_10d_ago": [20.0],
        "_close_20d_ago": [20.0],
        "_close_30d_ago": [20.0],
        "_close_60d_ago": [20.0],
        "_vol_19d_pct_sum": [0.0],
        "_vol_19d_pct_sq_sum": [0.0],
        "_adj_factor": [2.0],
    })
    today_ohlcv = pl.DataFrame({
        "symbol": ["600988.SH"],
        "date": [date(2026, 7, 3)],
        "open": [10.5],
        "high": [12.0],
        "low": [9.0],
        "close": [11.0],
        "volume": [1000.0],
        "amount": [11000.0],
        "prev_close": [10.0],
        "change_pct": [9.99],
        "change_amount": [99.9],
        "amplitude": [8.88],
        "turnover_rate": [None],
    })
    instruments = pl.DataFrame({
        "symbol": ["600988.SH"],
        "name": ["赤峰黄金"],
        "float_shares": [10_000.0],
    })

    out = compute_enriched_today(live_agg, pl.DataFrame(), today_ohlcv, instruments=instruments)
    row = out.to_dicts()[0]

    assert row["close"] == 22.0
    assert row["prev_close"] == 20.0
    assert abs(row["change_pct"] - 0.1) < 1e-12
    assert row["change_amount"] == 2.0
    assert row["amplitude"] == 0.3
    assert row["turnover_rate"] == 10.0


def test_tdx_quote_total_hand_maps_to_shares():
    provider = FQuantProvider.__new__(FQuantProvider)
    provider.name = "fquant_local"

    row = provider._tdx_quote_to_row({
        "Code": "600519",
        "K": {"Close": 10000, "Last": 9900, "Open": 9950, "High": 10100, "Low": 9900},
        "TotalHand": 123,
        "Amount": 456000,
        "ServerTime": "2026-07-01 10:00:00",
    })

    assert row is not None
    assert row["volume"] == 12_300
