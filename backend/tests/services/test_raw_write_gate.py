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


def test_index_etf_and_hk_raw_write_allowed_in_local_stock_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: True)
    r = repo(tmp_path)

    r.append_index_daily(DF.with_columns(pl.lit("000001.INDEX").alias("symbol")))
    r.append_etf_daily(DF.with_columns(pl.lit("510300.ETF").alias("symbol")))
    r.append_hk_daily(DF.with_columns(pl.lit("02577.HK").alias("symbol")))
    r.merge_live_daily_asset("index", DF.with_columns(pl.lit("000001.INDEX").alias("symbol")))
    r.flush_live_daily_asset("etf", DF.with_columns(pl.lit("510300.ETF").alias("symbol")))
    r.flush_live_daily_asset("hk", DF.with_columns(pl.lit("02577.HK").alias("symbol")))

    assert (tmp_path / "kline_index_daily" / "date=2026-07-01" / "part.parquet").exists()
    assert (tmp_path / "kline_etf_daily" / "date=2026-07-01" / "part.parquet").exists()
    assert (tmp_path / "kline_hk_daily" / "date=2026-07-01" / "part.parquet").exists()


def test_hk_enriched_write_and_read(tmp_path):
    """append_hk_enriched 现落 ENRICHED_STORAGE_COLS 全量(与 A 股/ETF 同一套契约),
    不再是旧版硬编码的 5 列窄表。change_pct 与 A 股/ETF 一致 —— 是即时派生列,
    不落盘,所以这里不再断言它能从存储里读回。见 tests/storage/test_repository_hk.py
    的落盘列回归测试。
    """
    r = repo(tmp_path)

    r.append_hk_enriched(
        DF.with_columns(
            pl.lit("02577.HK").alias("symbol"),
            pl.lit("fquant_local").alias("source"),
        )
    )

    df = r.get_hk_daily("02577.HK", date(2026, 7, 1), date(2026, 7, 1), ["symbol", "date", "close", "volume", "amount"])
    assert df.to_dicts() == [{
        "symbol": "02577.HK",
        "date": date(2026, 7, 1),
        "close": 1.0,
        "volume": 100.0,
        "amount": 100.0,
    }]
