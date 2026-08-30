from datetime import date
from pathlib import Path

from app.auction.contracts import AuctionFinal, AuctionSnapshot, UnmatchedSide, source_time_ms
from app.auction.repository import AuctionRepository


def test_repository_does_not_write_daily_kline(tmp_path: Path):
    repo = AuctionRepository(tmp_path)
    day = date(2026, 8, 20)
    snap = AuctionSnapshot(
        trade_date=day,
        symbol="000001.SZ",
        source="eltdx",
        source_time_ms=source_time_ms(day, 92000),
        received_at_ms=source_time_ms(day, 92001),
        indicative_price=10.2,
        matched_volume=100,
        unmatched_volume=20,
        unmatched_side=UnmatchedSide.buy,
        pre_close=10.0,
    )
    repo.append_snapshots([snap])
    repo.upsert_finals(
        [
            AuctionFinal(
                trade_date=day,
                symbol="000001.SZ",
                source="tushare",
                available_at_ms=source_time_ms(day, 92530),
                open_price=10.25,
                vwap=10.22,
                open_volume=200,
                open_amount=205000,
                pre_close=10.0,
                turnover_rate=1.2,
                volume_ratio=3.0,
                open_change_pct=0.025,
            )
        ]
    )
    assert (tmp_path / "auction" / "snapshots").exists()
    assert not (tmp_path / "kline_daily").exists()
    assert not (tmp_path / "kline_daily_enriched").exists()
    loaded = repo.load_snapshots(day)
    assert loaded.height == 1
    finals = repo.load_finals(day)
    assert finals.height == 1
    assert "000001.SZ" in repo.list_dates() or "2026-08-20" in repo.list_dates()
