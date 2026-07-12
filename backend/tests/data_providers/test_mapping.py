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

from app.data_providers.fquant.mapping import generated_minute_time, klines_rows_to_daily


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
    rows = [{"tdate": "2026-07-01", "open": 10, "high": 11, "low": 9, "close": 10.5,
              "cjl": 5000, "cje": 52500, "zf": 1.0}]
    out = klines_rows_to_daily(rows, "600519.SH", asset_type="stock")
    assert out[0]["volume"] == 500_000  # 5000 * 100


def test_klines_rows_to_daily_hk_uses_divide_by_10000():
    """Real hk00700 2025-10-20 values: cjl=14,963,400 vs verified real
    volume=1,496.0 (cross-checked against tdx-hk.duckdb market_day_kline)."""
    rows = [{"tdate": "2025-10-20", "open": 625.0, "high": 630.0, "low": 620.0,
              "close": 627.5, "cjl": 14_963_400, "cje": 9_384_000_000, "zf": 1.0}]
    out = klines_rows_to_daily(rows, "00700.HK", asset_type="hk")
    assert out[0]["volume"] == pytest.approx(1_496.34, rel=1e-4)
