from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl


def test_sync_services_produce_four_receipts_and_open_research_gate(
    tmp_path,
    monkeypatch,
):
    from app.data_providers import custom as custom_sources
    from app.data_providers.trust import (
        load_latest_audits,
        record_daily_enriched_audit,
    )
    from app.services import advisor, instrument_sync, kline_sync, preferences
    from app.tickflow.capabilities import CapabilitySet

    as_of = date(2026, 7, 24)
    symbols = ["600000.SH", "000001.SZ"]
    daily = pl.DataFrame(
        {
            "symbol": symbols,
            "date": [as_of, as_of],
            "open": [10.0, 11.0],
            "high": [10.5, 11.5],
            "low": [9.9, 10.9],
            "close": [10.2, 11.2],
            "volume": [1_000.0, 2_000.0],
            "amount": [1_020_000.0, 2_240_000.0],
        }
    )
    factors = pl.DataFrame(
        {
            "symbol": symbols,
            "trade_date": [date(2026, 6, 10), date(2026, 6, 11)],
            "ex_factor": [1.1, 1.05],
        }
    )

    class Provider:
        def get_instruments(self, asset_type):
            return [
                {
                    "symbol": symbol,
                    "name": symbol,
                    "code": symbol[:6],
                    "exchange": symbol[-2:],
                    "region": "CN",
                    "type": asset_type,
                    "ext": {"listing_date": "20000101", "list_status": "L"},
                }
                for symbol in symbols
            ]

        def get_daily(self, requested, start_time, end_time, on_chunk_done=None):
            assert requested == symbols
            return daily

        def get_adj_factors(
            self,
            requested,
            start_time,
            end_time,
            asset_type,
            on_chunk_done=None,
        ):
            assert requested == symbols
            return factors

    provider = Provider()
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "verified-source")
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "verified-source")
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: True)
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)

    persisted_daily: list[pl.DataFrame] = []
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        append_daily=persisted_daily.append,
        db=SimpleNamespace(execute=lambda statement: None),
    )

    assert instrument_sync.sync_instruments(tmp_path) == 2
    assert (
        kline_sync.sync_and_persist_daily_batch(
            symbols,
            repo,
            CapabilitySet(),
            end_date=as_of,
        )
        == 2
    )
    written_factors, affected = kline_sync.sync_adj_factor(
        symbols,
        repo,
        CapabilitySet(),
    )
    assert written_factors == 2
    assert set(affected) == set(symbols)

    enriched_path = (
        tmp_path
        / "kline_daily_enriched"
        / f"date={as_of.isoformat()}"
        / "part.parquet"
    )
    enriched_path.parent.mkdir(parents=True)
    daily.write_parquet(enriched_path)
    record_daily_enriched_audit(tmp_path, requested_symbols=symbols)

    audits = load_latest_audits(tmp_path)
    assert {receipt["dataset"] for receipt in audits} == {
        "instruments",
        "daily",
        "adj_factor",
        "daily_enriched",
    }

    strategy_row = {
        "symbol": symbols[0],
        "name": "浦发银行",
        "close": 10.2,
        "change_pct": 0.02,
        "score": 82.0,
        "status": "normal",
    }
    cache = {
        "as_of": as_of.isoformat(),
        "results": {
            "trend_breakout": {
                "as_of": as_of.isoformat(),
                "rows": [strategy_row],
            },
            "bullish_alignment": {
                "as_of": as_of.isoformat(),
                "rows": [{**strategy_row, "score": 76.0}],
            },
        },
    }

    result = advisor.build_advisor_recommendations(audits, cache)

    assert result["data_gate"]["decision"] == "PASS"
    assert result["candidates"][0]["decision"] == "GO"
