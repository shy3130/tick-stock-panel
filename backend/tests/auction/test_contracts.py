from datetime import date, datetime

from app.auction.contracts import (
    AuctionStage,
    UnmatchedSide,
    auction_stage,
    cn_datetime,
    is_open_auction_point,
    time_label_to_hhmmss,
    unmatched_side_from_raw,
)
from app.auction.sources import shares_to_hands
from app.market_time import CN_TZ


def test_auction_stage_windows():
    def at(h, m, s=0):
        return datetime(2026, 8, 20, h, m, s, tzinfo=CN_TZ)

    assert auction_stage(at(9, 14)) == AuctionStage.pre_open
    assert auction_stage(at(9, 15)) == AuctionStage.cancellable
    assert auction_stage(at(9, 20)) == AuctionStage.locked
    assert auction_stage(at(9, 25)) == AuctionStage.final
    assert auction_stage(at(9, 30)) == AuctionStage.post_open
    assert auction_stage(at(15, 0)) == AuctionStage.closed


def test_open_auction_point_excludes_close_auction():
    assert is_open_auction_point(91500)
    assert is_open_auction_point(92459)
    assert is_open_auction_point(92500)
    assert not is_open_auction_point(93000)
    assert not is_open_auction_point(145700)


def test_unmatched_side_and_tushare_volume_units():
    assert unmatched_side_from_raw(12) == UnmatchedSide.buy
    assert unmatched_side_from_raw(-3) == UnmatchedSide.sell
    assert unmatched_side_from_raw(0) == UnmatchedSide.neutral
    assert unmatched_side_from_raw(None) == UnmatchedSide.unknown
    assert shares_to_hands(45400) == 454.0
    assert time_label_to_hhmmss("09:20:03") == 92003
    assert cn_datetime(date(2026, 8, 20), 9, 25).tzinfo is not None
