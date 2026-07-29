from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest


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


def _seed_trusted_gate(tmp_path):
    from app.data_providers.trust import DataAudit, write_latest_audit

    as_of = "2026-07-24"
    for dataset in ("instruments", "daily", "adj_factor", "daily_enriched"):
        write_latest_audit(
            tmp_path,
            DataAudit(
                provider="derived" if dataset == "daily_enriched" else "verified-source",
                dataset=dataset,
                status="ok",
                row_count=2,
                returned_symbols=("600000.SH", "000001.SZ"),
                missing_symbols=(),
                coverage_ratio=1.0,
                observed_start="2026-06-10",
                observed_end=(
                    "2026-06-10"
                    if dataset == "adj_factor"
                    else as_of
                ),
            ),
        )
    row = {
        "symbol": "600000.SH",
        "name": "浦发银行",
        "close": 10.2,
        "change_pct": 0.02,
        "score": 82.0,
        "status": "normal",
    }
    return {
        "as_of": as_of,
        "results": {
            "trend_breakout": {"as_of": as_of, "rows": [row]},
            "bullish_alignment": {
                "as_of": as_of,
                "rows": [{**row, "score": 76.0}],
            },
        },
    }


def _assert_latest_error_blocks_gate(tmp_path, cache, dataset, provider):
    from app.data_providers.trust import load_latest_audits
    from app.services.advisor import build_advisor_recommendations

    audits = load_latest_audits(tmp_path)
    latest = {receipt["dataset"]: receipt for receipt in audits}
    assert latest[dataset]["status"] == "error"
    assert latest[dataset]["provider"] == provider
    result = build_advisor_recommendations(audits, cache)
    assert result["data_gate"]["decision"] == "BLOCK"
    assert result["candidates"][0]["decision"] == "NO-GO"


def test_unavailable_instrument_provider_overwrites_old_success_receipt(
    tmp_path,
    monkeypatch,
):
    from app.data_providers import custom as custom_sources
    from app.data_providers.trust import DataProviderUnavailable
    from app.services import instrument_sync, preferences

    cache = _seed_trusted_gate(tmp_path)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "missing-source")
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: False)

    with pytest.raises(DataProviderUnavailable):
        instrument_sync.sync_instruments(tmp_path)

    _assert_latest_error_blocks_gate(
        tmp_path,
        cache,
        "instruments",
        "missing-source",
    )


def test_unavailable_daily_dataset_overwrites_old_success_receipt(
    tmp_path,
    monkeypatch,
):
    from app.data_providers import custom as custom_sources
    from app.data_providers.trust import DataProviderUnavailable
    from app.services import kline_sync, preferences
    from app.tickflow.capabilities import CapabilitySet

    cache = _seed_trusted_gate(tmp_path)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "missing-source")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: False)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    with pytest.raises(DataProviderUnavailable):
        kline_sync.sync_and_persist_daily_batch(
            ["600000.SH"],
            repo,
            CapabilitySet(),
        )

    _assert_latest_error_blocks_gate(tmp_path, cache, "daily", "missing-source")


def test_unavailable_adjustment_dataset_overwrites_old_success_receipt(
    tmp_path,
    monkeypatch,
):
    from app.data_providers import custom as custom_sources
    from app.data_providers.trust import DataProviderUnavailable
    from app.services import kline_sync, preferences
    from app.tickflow.capabilities import CapabilitySet

    cache = _seed_trusted_gate(tmp_path)
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "missing-source")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: False)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    with pytest.raises(DataProviderUnavailable):
        kline_sync.sync_adj_factor(
            ["600000.SH"],
            repo,
            CapabilitySet(),
        )

    _assert_latest_error_blocks_gate(
        tmp_path,
        cache,
        "adj_factor",
        "missing-source",
    )


def test_provider_initialization_failures_overwrite_daily_and_factor_receipts(
    tmp_path,
    monkeypatch,
):
    from app.data_providers import custom as custom_sources
    from app.data_providers.trust import DataProviderUnavailable
    from app.services import kline_sync, preferences
    from app.tickflow.capabilities import CapabilitySet

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(
        custom_sources,
        "get_provider",
        lambda name: (_ for _ in ()).throw(RuntimeError("provider init failed")),
    )
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "broken-source")
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "broken-source")

    cache = _seed_trusted_gate(tmp_path)
    with pytest.raises(DataProviderUnavailable):
        kline_sync.sync_and_persist_daily_batch(
            ["600000.SH"],
            repo,
            CapabilitySet(),
        )
    _assert_latest_error_blocks_gate(tmp_path, cache, "daily", "broken-source")

    cache = _seed_trusted_gate(tmp_path)
    with pytest.raises(DataProviderUnavailable):
        kline_sync.sync_adj_factor(
            ["600000.SH"],
            repo,
            CapabilitySet(),
        )
    _assert_latest_error_blocks_gate(
        tmp_path,
        cache,
        "adj_factor",
        "broken-source",
    )


def test_missing_tickflow_capabilities_overwrite_old_success_receipts(
    tmp_path,
    monkeypatch,
):
    from app.services import kline_sync, preferences
    from app.tickflow.capabilities import CapabilitySet

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "tickflow")

    cache = _seed_trusted_gate(tmp_path)
    assert (
        kline_sync.sync_and_persist_daily_batch(
            ["600000.SH"],
            repo,
            CapabilitySet(),
        )
        == 0
    )
    _assert_latest_error_blocks_gate(tmp_path, cache, "daily", "tickflow")

    cache = _seed_trusted_gate(tmp_path)
    assert kline_sync.sync_adj_factor(
        ["600000.SH"],
        repo,
        CapabilitySet(),
    ) == (0, [])
    _assert_latest_error_blocks_gate(tmp_path, cache, "adj_factor", "tickflow")


def test_tickflow_adjustment_client_init_failure_overwrites_old_success_receipt(
    tmp_path,
    monkeypatch,
):
    from app.services import kline_sync, preferences
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "tickflow")
    monkeypatch.setattr(
        kline_sync,
        "get_client",
        lambda: (_ for _ in ()).throw(RuntimeError("tickflow init failed")),
    )

    cache = _seed_trusted_gate(tmp_path)
    assert kline_sync.sync_adj_factor(
        ["600000.SH"],
        repo,
        CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits(batch=10)}),
    ) == (0, [])

    _assert_latest_error_blocks_gate(
        tmp_path,
        cache,
        "adj_factor",
        "tickflow",
    )


def test_run_now_missing_tickflow_adjustment_capability_closes_trust_gate(
    tmp_path,
    monkeypatch,
):
    from app.jobs import daily_pipeline
    from app.services import preferences
    from app.tickflow.capabilities import CapabilitySet

    cache = _seed_trusted_gate(tmp_path)
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: date.today(),
    )

    monkeypatch.setattr(daily_pipeline.instrument_sync, "sync_instruments", lambda path: 0)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda capset: ["600000.SH"])
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda table=None: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda repo, name: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_views", lambda repo: None)
    monkeypatch.setattr(daily_pipeline, "run_pipeline", lambda **kwargs: 0)
    monkeypatch.setattr(preferences, "get_pipeline_pull_a_share", lambda: False)
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "tickflow")
    monkeypatch.setattr(preferences, "get_pipeline_pull_index", lambda: False)
    monkeypatch.setattr(preferences, "get_pipeline_pull_etf", lambda: False)
    monkeypatch.setattr(preferences, "get_minute_sync_enabled", lambda: False)
    monkeypatch.setattr(preferences, "get_minute_sync_days", lambda: 5)

    result = daily_pipeline.run_now(repo, CapabilitySet())

    assert "sync_adj" in result["skipped_stages"]
    _assert_latest_error_blocks_gate(
        tmp_path,
        cache,
        "adj_factor",
        "tickflow",
    )


def test_tickflow_instrument_exchange_failure_overwrites_old_success_receipt(
    tmp_path,
    monkeypatch,
):
    from app.services import instrument_sync, preferences

    class Exchanges:
        def get_instruments(self, exchange, instrument_type):
            if exchange == "SH":
                raise RuntimeError("SH endpoint failed")
            return [
                {
                    "symbol": f"000001.{exchange}",
                    "name": exchange,
                    "code": "000001",
                    "exchange": exchange,
                    "region": "CN",
                    "type": instrument_type,
                }
            ]

    cache = _seed_trusted_gate(tmp_path)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")
    monkeypatch.setattr(
        instrument_sync,
        "get_client",
        lambda: SimpleNamespace(exchanges=Exchanges()),
    )

    assert instrument_sync.sync_instruments(tmp_path) == 0

    _assert_latest_error_blocks_gate(tmp_path, cache, "instruments", "tickflow")
