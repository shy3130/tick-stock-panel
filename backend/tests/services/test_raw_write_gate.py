from datetime import date

import polars as pl

from app.storage.repository import DataStore, KlineRepository


DF = pl.DataFrame({
    "symbol": ["600519.SH"],
    "date": [date(2026, 7, 1)],
    "open": [1.0],
    "high": [1.0],
    "low": [1.0],
    "close": [1.0],
    "volume": [100.0],
    "amount": [100.0],
})


def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def test_raw_daily_write_skipped_in_local_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    r = repo(tmp_path)

    r.append_daily(DF)

    assert not (tmp_path / "kline_daily" / "date=2026-07-01" / "part.parquet").exists()


def test_raw_daily_write_allowed_outside_local_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    r = repo(tmp_path)

    r.append_daily(DF)

    assert (tmp_path / "kline_daily" / "date=2026-07-01" / "part.parquet").exists()


def test_enriched_write_not_gated(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    r = repo(tmp_path)

    r.append_enriched(DF.with_columns(pl.lit(1.0).alias("raw_close")))

    assert (tmp_path / "kline_daily_enriched" / "date=2026-07-01" / "part.parquet").exists()


def test_live_raw_write_skipped_in_local_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    r = repo(tmp_path)

    r.flush_live_daily(DF)
    r.merge_live_daily_asset("stock", DF)

    assert not (tmp_path / "kline_daily" / "date=2026-07-01" / "part.parquet").exists()


def test_index_and_etf_raw_write_allowed_in_local_stock_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    r = repo(tmp_path)

    r.append_index_daily(DF.with_columns(pl.lit("000001.INDEX").alias("symbol")))
    r.append_etf_daily(DF.with_columns(pl.lit("510300.ETF").alias("symbol")))
    r.merge_live_daily_asset("index", DF.with_columns(pl.lit("000001.INDEX").alias("symbol")))
    r.flush_live_daily_asset("etf", DF.with_columns(pl.lit("510300.ETF").alias("symbol")))

    assert (tmp_path / "kline_index_daily" / "date=2026-07-01" / "part.parquet").exists()
    assert (tmp_path / "kline_etf_daily" / "date=2026-07-01" / "part.parquet").exists()
