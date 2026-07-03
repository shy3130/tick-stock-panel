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
    lookup, uncovered = build_price_lookup([fill], tmp_path)
    assert lookup[("600000.SH", "2024-01-30")]["pos_20d"] == 1.0
    assert uncovered == []


def test_build_price_lookup_reads_hk_enriched(tmp_path):
    root = tmp_path / "kline_hk_enriched" / "date=2024-01-30"
    root.mkdir(parents=True)
    pl.DataFrame(
        [{"symbol": "02577.HK", "date": f"2024-01-{i:02d}", "close": float(i)} for i in range(1, 31)]
    ).write_parquet(root / "part.parquet")
    fill = Fill("2024-01-30", "", "02577.HK", "H", "buy", 100, 30.0, -3000.0, 1.0)
    lookup, uncovered = build_price_lookup([fill], tmp_path)
    assert lookup[("02577.HK", "2024-01-30")]["pos_20d"] == 1.0
    assert uncovered == []


def test_build_price_lookup_marks_history_short_as_uncovered(tmp_path):
    root = tmp_path / "kline_daily_enriched" / "date=2024-01-10"
    root.mkdir(parents=True)
    pl.DataFrame(
        [{"symbol": "600000.SH", "date": f"2024-01-{i:02d}", "close": float(i)} for i in range(1, 11)]
    ).write_parquet(root / "part.parquet")
    fill = Fill("2024-01-10", "", "600000.SH", "A", "buy", 100, 10.0, -1000.0, 1.0)
    assert build_price_lookup([fill], tmp_path) == ({}, ["600000.SH"])


def test_build_price_lookup_partial_coverage_is_not_uncovered(tmp_path):
    root = tmp_path / "kline_daily_enriched" / "date=2024-01-30"
    root.mkdir(parents=True)
    pl.DataFrame(
        [{"symbol": "600000.SH", "date": f"2024-01-{i:02d}", "close": float(i)} for i in range(1, 31)]
    ).write_parquet(root / "part.parquet")
    fills = [
        Fill("2024-01-10", "", "600000.SH", "A", "buy", 100, 10.0, -1000.0, 1.0),
        Fill("2024-01-30", "", "600000.SH", "A", "buy", 100, 30.0, -3000.0, 1.0),
    ]
    lookup, uncovered = build_price_lookup(fills, tmp_path)
    assert ("600000.SH", "2024-01-30") in lookup
    assert uncovered == []


def test_build_price_lookup_reads_etf_enriched(tmp_path):
    root = tmp_path / "kline_etf_enriched" / "date=2024-01-30"
    root.mkdir(parents=True)
    pl.DataFrame(
        [{"symbol": "513050.SH", "date": f"2024-01-{i:02d}", "close": float(i)} for i in range(1, 31)]
    ).write_parquet(root / "part.parquet")
    fill = Fill("2024-01-30", "", "513050.SH", "ETF", "buy", 100, 30.0, -3000.0, 1.0)
    lookup, uncovered = build_price_lookup([fill], tmp_path)
    assert lookup[("513050.SH", "2024-01-30")]["pos_20d"] == 1.0
    assert uncovered == []


def test_build_price_lookup_ignores_empty_existing_dirs(tmp_path):
    (tmp_path / "kline_etf_enriched").mkdir()
    root = tmp_path / "kline_daily_enriched" / "date=2024-01-30"
    root.mkdir(parents=True)
    pl.DataFrame(
        [{"symbol": "600000.SH", "date": f"2024-01-{i:02d}", "close": float(i)} for i in range(1, 31)]
    ).write_parquet(root / "part.parquet")
    fill = Fill("2024-01-30", "", "600000.SH", "A", "buy", 100, 30.0, -3000.0, 1.0)
    lookup, uncovered = build_price_lookup([fill], tmp_path)
    assert ("600000.SH", "2024-01-30") in lookup
    assert uncovered == []


def test_build_price_lookup_deduplicates_enriched_and_raw_before_rolling(tmp_path):
    for dirname in ("kline_daily_enriched", "kline_daily"):
        root = tmp_path / dirname / "date=2024-01-15"
        root.mkdir(parents=True)
        pl.DataFrame(
            [{"symbol": "000988.SZ", "date": f"2024-01-{i:02d}", "close": float(i)} for i in range(1, 16)]
        ).write_parquet(root / "part.parquet")

    fill = Fill("2024-01-15", "", "000988.SZ", "A", "buy", 100, 15.0, -1500.0, 1.0)
    lookup, uncovered = build_price_lookup([fill], tmp_path)
    assert lookup == {}
    assert uncovered == ["000988.SZ"]
