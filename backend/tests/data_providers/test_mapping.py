"""Tests for app.data_providers.fquant.mapping.

Covers two regression fixes found in the same session:
- klines_rows_to_daily: fstore cjl -> volume multiplier must be market-aware
  (A股 x100, 港股 /10000), not a blanket x100.
- generated_minute_time: HK's session windows (150 AM + 180 PM) differ from
  A-share's (120 AM + 120 PM); reusing A-share's split mislabels HK PM-session
  minutes. Verified against the same HK session boundaries confirmed with
  real tdx-hkminutes.duckdb data in the engine repo's tdx-kline feature.
"""
from __future__ import annotations

import pytest

from app.data_providers.fquant.mapping import (
    generated_minute_time,
    klines_rows_to_daily,
    wide_rows_to_daily,
)


def test_generated_minute_time_a_share_unchanged():
    assert generated_minute_time(0, "20260710") == "2026-07-10 09:31:00"
    assert generated_minute_time(119, "20260710") == "2026-07-10 11:30:00"
    assert generated_minute_time(120, "20260710") == "2026-07-10 13:01:00"
    assert generated_minute_time(239, "20260710") == "2026-07-10 15:00:00"


def test_generated_minute_time_hk_uses_150_180_split():
    assert generated_minute_time(0, "20260710", asset_type="hk") == "2026-07-10 09:31:00"
    assert generated_minute_time(149, "20260710", asset_type="hk") == "2026-07-10 12:00:00"
    assert generated_minute_time(150, "20260710", asset_type="hk") == "2026-07-10 13:01:00"
    assert generated_minute_time(329, "20260710", asset_type="hk") == "2026-07-10 16:00:00"


def test_generated_minute_time_hk_index_120_still_morning():
    """The bug this guards against: index 120 is still HK's morning session
    (150-minute AM block), not the start of PM like it is for A-share."""
    result = generated_minute_time(120, "20260710", asset_type="hk")
    assert result == "2026-07-10 11:31:00"
    assert result != generated_minute_time(120, "20260710")  # differs from A-share


def test_klines_rows_to_daily_stock_multiplier_unchanged():
    """Real 000001 2025-10-20 fstore row: cje/close = 94,731,175 implied
    shares vs cjl=952,641 -> ratio 99.44, confirming the x100 multiplier."""
    rows = [{"tdate": "2025-10-20", "open": 12.0, "high": 12.5, "low": 11.8,
              "close": 12.34, "cjl": 952_641, "cje": 1_168_982_700.0, "zf": 1.0}]
    out = klines_rows_to_daily(rows, "000001.SZ", asset_type="stock")
    volume = out[0]["volume"]

    assert volume == 95_264_100  # 952,641 * 100

    # Same physical invariant as the HK case: volume must be ~ amount/close.
    implied_shares = out[0]["amount"] / out[0]["close"]
    assert volume == pytest.approx(implied_shares, rel=0.01)


def test_klines_rows_to_daily_hk_cjl_is_already_shares():
    """Real 00700 2025-10-20 fstore row. The physical constraint
    cje/close = shares gives 9,379,380,224 / 627.5 = 14,947,219 shares,
    which matches cjl (14,963,400) at ratio 0.9989 -> cjl is ALREADY shares
    for HK, multiplier is x1.

    Guards against both historical bugs: the original blanket x100 (100x
    overstatement) and the later "align to tdx-hk market_day_kline.volume"
    /10000 attempt (which yields *lots*, not shares -- 10000x understatement).
    """
    rows = [{"tdate": "2025-10-20", "open": 625.0, "high": 630.0, "low": 620.0,
              "close": 627.5, "cjl": 14_963_400, "cje": 9_379_380_224.0, "zf": 1.0}]
    out = klines_rows_to_daily(rows, "00700.HK", asset_type="hk")
    volume = out[0]["volume"]

    assert volume == 14_963_400  # x1, unchanged

    # The invariant that actually pins this down, independent of any volume column:
    implied_shares = out[0]["amount"] / out[0]["close"]
    assert volume == pytest.approx(implied_shares, rel=0.01)


def test_daily_mapping_converts_non_finite_numbers_to_null():
    rows = [{
        "date": "2026-08-10",
        "open": float("nan"),
        "high": float("inf"),
        "low": -float("inf"),
        "close": 10.0,
    }]
    out = wide_rows_to_daily(rows, "600519.SH")
    assert out[0]["open"] is None
    assert out[0]["high"] is None
    assert out[0]["low"] is None
    assert out[0]["close"] == 10.0
