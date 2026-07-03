import polars as pl

from app.services.trade_journal.models import Fill
from app.services.trade_journal.pricepos import build_price_lookup


def test_build_price_lookup_reads_enriched_parquet(tmp_path):
    root = tmp_path / "kline_daily_enriched" / "date=2024-01-30"
    root.mkdir(parents=True)
    rows = [
        {"symbol": "600000.SH", "date": f"2024-01-{i:02d}", "close": float(i)}
        for i in range(1, 31)
    ]
    pl.DataFrame(rows).write_parquet(root / "part.parquet")
    fill = Fill("2024-01-30", "", "600000.SH", "A", "buy", 100, 30.0, -3000.0, 1.0)
    lookup = build_price_lookup([fill], tmp_path)
    assert lookup[("600000.SH", "2024-01-30")]["pos_20d"] == 1.0


def test_build_price_lookup_skips_hk(tmp_path):
    fill = Fill("2024-01-30", "", "02577.HK", "H", "buy", 100, 30.0, -3000.0, 1.0)
    assert build_price_lookup([fill], tmp_path) == {}
