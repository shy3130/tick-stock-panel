from __future__ import annotations

import pytest

from research.selection.auction import AuctionOverlayConfig, apply_auction_overlay


def _base(symbol: str, rank: int, industry: str = "行业A") -> dict:
    return {
        "signal_date": "2026-08-07",
        "symbol": symbol,
        "name": symbol,
        "industry_current": industry,
        "eligible_rank": rank,
        "score": 60 - rank,
    }


def _auction(symbol: str, *, price: float = 10.1, amount: float = 2_000_000, ratio: float = 1.0) -> dict:
    return {
        "ts_code": symbol,
        "trade_date": "20260810",
        "price": price,
        "pre_close": 10.0,
        "amount": amount,
        "vol": 100_000,
        "volume_ratio": ratio,
        "turnover_rate": 0.02,
    }


def test_overlay_confirms_and_preserves_base_rank() -> None:
    rows, summary = apply_auction_overlay(
        [_base("B.SZ", 2), _base("A.SH", 1)],
        [_auction("A.SH"), _auction("B.SZ")],
    )
    assert [row["symbol"] for row in rows] == ["A.SH", "B.SZ"]
    assert all(row["auction_status"] == "CONFIRMED" for row in rows)
    assert summary["selected_symbols"] == ["A.SH", "B.SZ"]


def test_overlay_rejects_chasing_and_keeps_weak_confirmation_on_watch() -> None:
    rows, summary = apply_auction_overlay(
        [_base("CHASE.SH", 1), _base("QUIET.SZ", 2)],
        [_auction("CHASE.SH", price=10.6), _auction("QUIET.SZ", amount=500_000, ratio=0.2)],
    )
    assert rows[0]["auction_status"] == "REJECT_CHASE"
    assert rows[1]["auction_status"] == "WATCH"
    assert summary["selected_count"] == 0


def test_overlay_applies_industry_cap_only_to_confirmed_candidates() -> None:
    config = AuctionOverlayConfig(max_positions=3, max_per_industry=1)
    rows, summary = apply_auction_overlay(
        [_base("A.SH", 1), _base("B.SH", 2), _base("C.SH", 3, "行业B")],
        [_auction("A.SH"), _auction("B.SH"), _auction("C.SH")],
        config=config,
    )
    assert summary["selected_symbols"] == ["A.SH", "C.SH"]
    assert rows[1]["portfolio_reason"] == "行业集中度限制：行业A已达1只"


def test_overlay_records_missing_and_rejects_duplicate_auction_rows() -> None:
    rows, summary = apply_auction_overlay([_base("A.SH", 1)], [])
    assert rows[0]["auction_status"] == "REJECT_MISSING"
    assert summary["auction_matched_count"] == 0
    with pytest.raises(ValueError, match="duplicate auction symbol"):
        apply_auction_overlay([_base("A.SH", 1)], [_auction("A.SH"), _auction("A.SH")])
