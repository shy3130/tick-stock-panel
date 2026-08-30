from datetime import date

from app.plugins.eltdx.provider import _auction_final, _auction_snapshots


def test_eltdx_auction_mapping_units():
    payload = {
        "pre_close_price": 10.0,
        "open_price": 10.5,
        "open_volume": 123.0,
        "open_amount": 128000.0,
        # eltdx open_change_pct 是百分数 (SDK _pct = (p-base)/base*100)
        "open_change_pct": 5.0,
        "snapshot_0925": {"price": 10.5, "volume": 123.0, "trade_amount_yuan": 128000.0},
        "series": {
            "points": [
                {
                    "time_label": "09:18:00",
                    "price": 10.2,
                    "matched_volume": 80.0,
                    "unmatched_volume": 40.0,
                    "unmatched_signed_raw": 12,
                    "matched_amount_estimated": 81600.0,
                },
                {
                    "time_label": "14:57:00",
                    "price": 11.0,
                    "matched_volume": 999.0,
                    "unmatched_volume": 1.0,
                    "unmatched_signed_raw": -1,
                },
            ]
        },
    }
    day = date(2026, 8, 20)
    snaps = _auction_snapshots(payload, "000001.SZ", day, 1, historical=True)
    assert len(snaps) == 1
    assert snaps[0].matched_volume == 80.0
    assert snaps[0].unmatched_side.value == "buy"
    assert "historical_backfill" in snaps[0].quality_flags
    final = _auction_final(payload, "000001.SZ", day, 1)
    assert final is not None
    assert final.open_price == 10.5
    assert final.open_volume == 123.0
    assert abs(final.open_change_pct - 0.05) < 1e-9


def test_historical_backfill_received_at_uses_source_time():
    """历史回填点 received_at_ms 应等于源时刻, 否则被 default_as_of_ms(09:25:30) 过滤丢弃。"""
    from app.auction.contracts import source_time_ms
    from app.auction.features import build_latest_features

    payload = {
        "pre_close_price": 10.0,
        "series": {
            "points": [
                {"time_label": "09:18:00", "price": 10.2, "matched_volume": 80.0,
                 "unmatched_volume": 40.0, "unmatched_signed_raw": 12},
                {"time_label": "09:22:00", "price": 10.4, "matched_volume": 160.0,
                 "unmatched_volume": 30.0, "unmatched_signed_raw": 5},
            ]
        },
    }
    day = date(2026, 8, 20)
    snaps = _auction_snapshots(payload, "000001.SZ", day, 999, historical=True)
    assert len(snaps) == 2
    assert snaps[0].received_at_ms == source_time_ms(day, 91800)
    # 历史日 as_of 默认 09:25:30, 过程点应全部进入特征而非被过滤。
    rows = build_latest_features(snaps, as_of_ms=source_time_ms(day, 92530), trade_date=day)
    assert len(rows) == 1
    assert rows[0]["point_count"] == 2
    assert rows[0]["indicative_price"] == 10.4


def test_open_change_pct_decimal_not_percent():
    """eltdx open_change_pct 是百分数, 必须 /100 转小数制。"""
    payload = {
        "pre_close_price": 10.0,
        "open_price": 10.5,
        "open_change_pct": 5.0,  # 5%
    }
    final = _auction_final(payload, "000001.SZ", date(2026, 8, 20), 1)
    assert final.open_change_pct == 0.05


def test_open_change_pct_falls_back_to_derived_decimal():
    payload = {"pre_close_price": 10.0, "open_price": 10.5}
    final = _auction_final(payload, "000001.SZ", date(2026, 8, 20), 1)
    assert abs(final.open_change_pct - 0.05) < 1e-9
