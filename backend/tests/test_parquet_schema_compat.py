from datetime import date

import polars as pl

from app.parquet import scan_daily_parquet, scan_enriched_parquet


def test_partitioned_daily_scan_tolerates_added_quote_ts(tmp_path):
    old_part = tmp_path / "kline_daily" / "date=2026-07-08" / "part.parquet"
    new_part = tmp_path / "kline_daily" / "date=2026-07-09" / "part.parquet"
    old_part.parent.mkdir(parents=True)
    new_part.parent.mkdir(parents=True)

    pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [date(2026, 7, 8)],
        "open": [10.0],
        "high": [10.5],
        "low": [9.8],
        "close": [10.2],
        "volume": [1000.0],
        "amount": [10200.0],
    }).write_parquet(old_part)

    pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [date(2026, 7, 9)],
        "open": [10.2],
        "high": [10.8],
        "low": [10.1],
        "close": [10.6],
        "volume": [1200],
        "amount": [12720.0],
        "quote_ts": [1783560600000],
    }).write_parquet(new_part)

    df = scan_daily_parquet(str(tmp_path / "kline_daily" / "**" / "*.parquet")).sort("date").collect()

    assert df.height == 2
    assert df.schema["volume"] == pl.Float64
    assert df.schema["quote_ts"] == pl.Int64
    assert df["quote_ts"].to_list() == [None, 1783560600000]


def test_partitioned_enriched_scan_tolerates_added_quote_ts(tmp_path):
    base = tmp_path / "kline_daily_enriched"
    old_part = base / "date=2026-07-08" / "part.parquet"
    new_part = base / "date=2026-07-09" / "part.parquet"
    old_part.parent.mkdir(parents=True)
    new_part.parent.mkdir(parents=True)

    common_old = {
        "symbol": ["600000.SH"],
        "date": [date(2026, 7, 8)],
        "open": [10.0],
        "high": [10.5],
        "low": [9.8],
        "close": [10.2],
        "volume": [1000.0],
        "amount": [10200.0],
        "raw_close": [10.2],
        "raw_high": [10.5],
        "raw_low": [9.8],
        "turnover_rate": [1.1],
        "consecutive_limit_ups": pl.Series([0], dtype=pl.UInt32),
        "consecutive_limit_downs": pl.Series([0], dtype=pl.UInt32),
    }
    pl.DataFrame(common_old).write_parquet(old_part)

    common_new = dict(common_old)
    common_new["date"] = [date(2026, 7, 9)]
    common_new["volume"] = [1200]
    common_new["quote_ts"] = [1783560600000]
    pl.DataFrame(common_new).write_parquet(new_part)

    df = scan_enriched_parquet(str(base / "**" / "*.parquet")).sort("date").collect()

    assert df.height == 2
    assert df.schema["volume"] == pl.Float64
    assert df.schema["quote_ts"] == pl.Int64
    assert df["quote_ts"].to_list() == [None, 1783560600000]
