from __future__ import annotations

from datetime import date

import polars as pl

from app.services.market_scope import (
    filter_frame_by_market,
    market_cache_key,
    market_currency,
    normalize_market,
)


def _mixed_market_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["600000.SH", "000001.SZ", "430017.BJ", "00700.HK", "AAPL.US"],
            "close": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
    )


def test_normalize_market_and_currency_contract() -> None:
    assert normalize_market("HK") == "hk"
    assert normalize_market("unknown") == "cn"
    assert normalize_market(None) == "cn"
    assert market_currency("cn") == "CNY"
    assert market_currency("hk") == "HKD"
    assert market_currency("us") == "USD"


def test_filter_frame_by_market_never_leaks_other_markets() -> None:
    frame = _mixed_market_frame()

    assert filter_frame_by_market(frame, "cn").get_column("symbol").to_list() == [
        "600000.SH",
        "000001.SZ",
        "430017.BJ",
    ]
    assert filter_frame_by_market(frame, "hk").get_column("symbol").to_list() == ["00700.HK"]
    assert filter_frame_by_market(frame, "us").get_column("symbol").to_list() == ["AAPL.US"]


def test_market_cache_key_isolates_same_date_between_markets() -> None:
    target = date(2026, 7, 17)

    assert market_cache_key("cn", target) == "cn:2026-07-17"
    assert market_cache_key("hk", target) == "hk:2026-07-17"
    assert market_cache_key("us", None) == "us:latest"
