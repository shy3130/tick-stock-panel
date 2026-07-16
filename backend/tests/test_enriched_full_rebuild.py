from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.indicators import pipeline


def _write_daily(data_dir, ds: str, close: float) -> None:
    out = data_dir / "kline_daily" / f"date={ds}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [date.fromisoformat(ds)],
        "open": [close],
        "high": [close],
        "low": [close],
        "close": [close],
        "volume": [100.0],
        "amount": [1000.0],
        "quote_ts": [0],
    }).write_parquet(out)


def _write_existing(data_dir, ds: str, close: float) -> None:
    out = data_dir / "kline_daily_enriched" / f"date={ds}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [date.fromisoformat(ds)],
        "close": [close],
    }).write_parquet(out)


def _fake_compute_enriched(raw: pl.DataFrame, **_kwargs) -> pl.DataFrame:
    return raw.with_columns(
        pl.col("close").alias("raw_close"),
        pl.col("high").alias("raw_high"),
        pl.col("low").alias("raw_low"),
        pl.lit(None, dtype=pl.Float64).alias("turnover_rate"),
        pl.lit(0, dtype=pl.UInt32).alias("consecutive_limit_ups"),
        pl.lit(0, dtype=pl.UInt32).alias("consecutive_limit_downs"),
    )


def test_full_rebuild_overwrites_existing_partitions_without_deleting_base(tmp_path, monkeypatch):
    _write_daily(tmp_path, "2026-07-14", 14.0)
    _write_daily(tmp_path, "2026-07-15", 15.0)
    _write_existing(tmp_path, "2026-07-15", 1.0)
    marker = tmp_path / "kline_daily_enriched" / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(pipeline, "compute_enriched", _fake_compute_enriched)

    written = pipeline.run_pipeline(data_dir=tmp_path)

    assert written == 2
    assert marker.read_text(encoding="utf-8") == "keep"
    assert pl.read_parquet(
        tmp_path / "kline_daily_enriched" / "date=2026-07-14" / "part.parquet"
    )["close"].to_list() == [14.0]
    assert pl.read_parquet(
        tmp_path / "kline_daily_enriched" / "date=2026-07-15" / "part.parquet"
    )["close"].to_list() == [15.0]


def test_full_rebuild_rejects_missing_existing_dates_before_writing(tmp_path, monkeypatch):
    _write_daily(tmp_path, "2026-07-15", 15.0)
    _write_existing(tmp_path, "2026-07-14", 14.0)
    _write_existing(tmp_path, "2026-07-15", 1.0)
    monkeypatch.setattr(pipeline, "compute_enriched", _fake_compute_enriched)

    with pytest.raises(RuntimeError, match="缺少已有日期分区"):
        pipeline.run_pipeline(data_dir=tmp_path)

    existing = pl.read_parquet(
        tmp_path / "kline_daily_enriched" / "date=2026-07-15" / "part.parquet"
    )
    assert existing["close"].to_list() == [1.0]
