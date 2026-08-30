from datetime import date

from app.auction.contracts import (
    AuctionSnapshot,
    UnmatchedSide,
    cn_datetime,
    datetime_to_ms,
    source_time_ms,
)
from app.auction.features import build_latest_features
from app.auction.ranking import rank_features


def _snap(symbol, hhmmss, *, received_hhmmss=None, price=10.3, matched=1000, unmatched=200, side=UnmatchedSide.buy, seq=0):
    day = date(2026, 8, 20)
    source_ms = source_time_ms(day, hhmmss)
    recv = source_time_ms(day, received_hhmmss or hhmmss)
    return AuctionSnapshot(
        trade_date=day,
        symbol=symbol,
        source="eltdx",
        source_time_ms=source_ms,
        received_at_ms=recv,
        indicative_price=price,
        matched_volume=matched,
        unmatched_volume=unmatched,
        unmatched_side=side,
        sequence=seq,
        pre_close=10.0,
    )


def test_as_of_truncation_hides_later_points():
    points = [
        _snap("000001.SZ", 91500, price=10.1, matched=100),
        _snap("000001.SZ", 92000, price=10.4, matched=400),
        _snap("000001.SZ", 92500, price=10.9, matched=900, received_hhmmss=92510),
    ]
    as_of = source_time_ms(date(2026, 8, 20), 92000)
    rows = build_latest_features(points, as_of_ms=as_of, trade_date=date(2026, 8, 20))
    assert len(rows) == 1
    assert rows[0]["point_count"] == 2
    assert rows[0]["indicative_price"] == 10.4
    assert rows[0]["gap_pct"] == 0.040000000000000036 or abs(rows[0]["gap_pct"] - 0.04) < 1e-9


def test_close_auction_points_are_ignored():
    points = [
        _snap("000001.SZ", 91500),
        AuctionSnapshot(
            trade_date=date(2026, 8, 20),
            symbol="000001.SZ",
            source="eltdx",
            source_time_ms=source_time_ms(date(2026, 8, 20), 145700),
            received_at_ms=source_time_ms(date(2026, 8, 20), 145700),
            indicative_price=11.0,
            matched_volume=9999,
            unmatched_volume=1,
            unmatched_side=UnmatchedSide.buy,
            pre_close=10.0,
        ),
    ]
    as_of = datetime_to_ms(cn_datetime(date(2026, 8, 20), 15, 0))
    rows = build_latest_features(points, as_of_ms=as_of, trade_date=date(2026, 8, 20))
    assert rows[0]["point_count"] == 1
    assert rows[0]["matched_volume"] == 1000


def test_same_time_points_do_not_explode_rates():
    # 两点 source_time 相同 (仅 sequence 不同) 时, 分钟跨度退化为 None,
    # 不应除以 1e-6 产生爆炸的斜率/增速。
    day = date(2026, 8, 20)
    points = [
        _snap("000001.SZ", 92000, seq=0, matched=100, price=10.1),
        _snap("000001.SZ", 92000, seq=1, matched=200, price=10.3),
    ]
    rows = build_latest_features(points, as_of_ms=source_time_ms(day, 92001), trade_date=day)
    assert rows[0]["price_slope_bps_per_minute"] is None
    assert rows[0]["matched_growth_rate_per_minute"] is None


def test_ranking_does_not_use_daily_close_fields():
    rows = [
        {
            "symbol": "000001.SZ",
            "gap_pct": 0.07,
            "buy_unmatched_persistence": 0.9,
            "unmatched_match_ratio": 0.5,
            "log_matched": 8.0,
            "log_growth": 5.0,
            "price_slope_bps_per_minute": 12,
            "price_stability_bps": 8,
            "max_drawdown_bps": 5,
            "unmatched_direction_switches": 0,
            "quality_score": 90,
            "change_pct": 0.99,
            "vol_ratio_5d": 99,
        }
    ]
    ranked = rank_features(rows, style="limit_up", limit=10)
    assert ranked[0]["score"] > 50
    assert "change_pct" not in ranked[0]["reasons"]
