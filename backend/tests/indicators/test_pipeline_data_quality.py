from datetime import date

import polars as pl

from app.data_providers.fquant_provider import FQuantProvider
from app.indicators.pipeline import compute_enriched, filter_halt_days


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
