from __future__ import annotations

import polars as pl

from app.jobs import daily_pipeline
from scripts.backfill_three_markets import select_market_symbols
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def test_custom_daily_provider_uses_all_markets_from_instruments(tmp_path, monkeypatch) -> None:
    instrument_dir = tmp_path / "instruments"
    instrument_dir.mkdir()
    pl.DataFrame(
        {"symbol": ["600000.SH", "00700.HK", "AAPL.US"]},
    ).write_parquet(instrument_dir / "instruments.parquet")
    monkeypatch.setattr(daily_pipeline.settings, "data_dir", tmp_path)
    monkeypatch.setattr(daily_pipeline._prefs, "get_daily_data_provider", lambda: "clickhouse")
    monkeypatch.setattr(daily_pipeline, "get_pool", lambda *_args, **_kwargs: ["600000.SH"])

    capset = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(batch=100)})

    assert daily_pipeline._resolve_universe(capset) == ["00700.HK", "600000.SH", "AAPL.US"]


def test_backfill_symbol_selection_is_market_scoped() -> None:
    instruments = pl.DataFrame({"symbol": ["600000.SH", "00700.HK", "AAPL.US"]})

    assert select_market_symbols(instruments, {"hk", "us"}) == ["00700.HK", "AAPL.US"]
