from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import polars as pl
import pytest


def test_daily_audit_marks_missing_requested_symbols_as_partial():
    try:
        from app.data_providers.trust import audit_market_frame
    except ImportError:
        pytest.fail("audit_market_frame is not implemented")

    frame = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "date": [date(2026, 7, 24)],
            "open": [10.0],
            "high": [10.5],
            "low": [9.9],
            "close": [10.2],
            "volume": [1_000.0],
            "amount": [1_020_000.0],
        }
    )

    audit = audit_market_frame(
        provider="tushare",
        dataset="daily",
        frame=frame,
        requested_symbols=["600000.SH", "000001.SZ"],
        requested_end=date(2026, 7, 24),
    )

    assert audit.status == "partial"
    assert audit.row_count == 1
    assert audit.returned_symbols == ("600000.SH",)
    assert audit.missing_symbols == ("000001.SZ",)
    assert audit.coverage_ratio == pytest.approx(0.5)
    assert audit.fallback_used is False
    assert audit.synthetic is False


def test_daily_audit_rejects_impossible_ohlc_rows():
    from app.data_providers.trust import audit_market_frame

    frame = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "date": [date(2026, 7, 24)],
            "open": [10.0],
            "high": [10.1],
            "low": [9.9],
            "close": [10.2],
            "volume": [1_000.0],
            "amount": [1_020_000.0],
        }
    )

    audit = audit_market_frame(
        provider="akshare",
        dataset="daily",
        frame=frame,
        requested_symbols=["600000.SH"],
        requested_end=date(2026, 7, 24),
    )

    assert audit.status == "invalid"
    assert audit.issues == ("daily.invalid_ohlc:1",)


def test_daily_audit_rejects_missing_canonical_columns():
    from app.data_providers.trust import audit_market_frame

    frame = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "date": [date(2026, 7, 24)],
            "close": [10.2],
        }
    )

    audit = audit_market_frame(
        provider="tushare",
        dataset="daily",
        frame=frame,
        requested_symbols=["600000.SH"],
        requested_end=date(2026, 7, 24),
    )

    assert audit.status == "invalid"
    assert audit.issues == (
        "daily.missing_columns:amount,high,low,open,volume",
    )


def test_daily_audit_rejects_rows_after_requested_as_of_date():
    from app.data_providers.trust import audit_market_frame

    frame = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "date": [date(2026, 7, 25)],
            "open": [10.0],
            "high": [10.5],
            "low": [9.9],
            "close": [10.2],
            "volume": [1_000.0],
            "amount": [1_020_000.0],
        }
    )

    audit = audit_market_frame(
        provider="tushare",
        dataset="daily",
        frame=frame,
        requested_symbols=["600000.SH"],
        requested_end=date(2026, 7, 24),
    )

    assert audit.status == "invalid"
    assert audit.issues == ("daily.after_requested_end:1",)


def test_daily_audit_exposes_actual_observed_date_range():
    from app.data_providers.trust import audit_market_frame

    frame = pl.DataFrame(
        {
            "symbol": ["600000.SH", "600000.SH"],
            "date": [date(2026, 7, 23), date(2026, 7, 24)],
            "open": [10.0, 10.1],
            "high": [10.5, 10.6],
            "low": [9.9, 10.0],
            "close": [10.2, 10.3],
            "volume": [1_000.0, 1_100.0],
            "amount": [1_020_000.0, 1_133_000.0],
        }
    )

    audit = audit_market_frame(
        provider="tushare",
        dataset="daily",
        frame=frame,
        requested_symbols=["600000.SH"],
        requested_end=date(2026, 7, 24),
    )

    assert audit.observed_start == "2026-07-23"
    assert audit.observed_end == "2026-07-24"


def test_persisted_daily_enriched_partition_records_actual_derived_coverage(
    tmp_path,
):
    try:
        from app.data_providers.trust import (
            load_latest_audits,
            record_daily_enriched_audit,
        )
    except ImportError:
        pytest.fail("the derived daily_enriched receipt is not implemented")

    for observed_date, symbols in [
        (date(2026, 7, 23), ["600000.SH"]),
        (date(2026, 7, 24), ["600000.SH", "000001.SZ"]),
    ]:
        out = (
            tmp_path
            / "kline_daily_enriched"
            / f"date={observed_date.isoformat()}"
            / "part.parquet"
        )
        out.parent.mkdir(parents=True)
        pl.DataFrame(
            {
                "symbol": symbols,
                "date": [observed_date] * len(symbols),
                "open": [10.0] * len(symbols),
                "high": [10.5] * len(symbols),
                "low": [9.9] * len(symbols),
                "close": [10.2] * len(symbols),
                "volume": [1_000.0] * len(symbols),
                "amount": [1_020_000.0] * len(symbols),
            }
        ).write_parquet(out)

    audit = record_daily_enriched_audit(
        tmp_path,
        requested_symbols=["600000.SH", "000001.SZ", "600519.SH"],
    )

    assert audit.provider == "derived"
    assert audit.dataset == "daily_enriched"
    assert audit.status == "partial"
    assert audit.coverage_ratio == pytest.approx(2 / 3)
    assert audit.missing_symbols == ("600519.SH",)
    assert audit.observed_start == "2026-07-24"
    assert audit.observed_end == "2026-07-24"
    receipts = {item["dataset"]: item for item in load_latest_audits(tmp_path)}
    assert receipts["daily_enriched"]["provider"] == "derived"
    assert receipts["daily_enriched"]["coverage_ratio"] == pytest.approx(2 / 3)


def test_selected_daily_provider_without_dataset_never_falls_back(
    tmp_path,
    monkeypatch,
):
    try:
        from app.data_providers.trust import DataProviderUnavailable
    except ImportError:
        pytest.fail("DataProviderUnavailable is not implemented")
    from app.data_providers import custom as custom_sources
    from app.services import kline_sync, preferences
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "akshare")
    monkeypatch.setattr(
        custom_sources,
        "provider_has_dataset",
        lambda provider, dataset: False,
    )

    def forbidden_tickflow_fetch(*args, **kwargs):
        pytest.fail("selected provider must not silently fall back to TickFlow")

    monkeypatch.setattr(kline_sync, "sync_daily_batch", forbidden_tickflow_fetch)
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        append_daily=lambda frame: pytest.fail("invalid data must not be persisted"),
    )
    capset = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(batch=10)})

    with pytest.raises(DataProviderUnavailable, match=r"akshare.*daily"):
        kline_sync.sync_and_persist_daily_batch(
            ["600000.SH"],
            repo,
            capset,
        )


def test_latest_audit_survives_process_restart(tmp_path):
    try:
        from app.data_providers.trust import load_latest_audits, write_latest_audit
    except ImportError:
        pytest.fail("the persistent data-audit ledger is not implemented")
    from app.data_providers.trust import audit_market_frame

    frame = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "date": [date(2026, 7, 24)],
            "open": [10.0],
            "high": [10.5],
            "low": [9.9],
            "close": [10.2],
            "volume": [1_000.0],
            "amount": [1_020_000.0],
        }
    )
    audit = audit_market_frame(
        provider="tushare",
        dataset="daily",
        frame=frame,
        requested_symbols=["600000.SH"],
        requested_end=date(2026, 7, 24),
    )

    write_latest_audit(
        tmp_path,
        audit,
        recorded_at=datetime(2026, 7, 24, 16, 0, tzinfo=UTC),
    )
    restored = load_latest_audits(tmp_path)

    assert restored == [
        {
            "schema_version": 1,
            "provider": "tushare",
            "dataset": "daily",
            "status": "ok",
            "row_count": 1,
            "returned_symbols": ["600000.SH"],
            "missing_symbols": [],
            "coverage_ratio": 1.0,
            "fallback_used": False,
            "synthetic": False,
            "issues": [],
            "observed_start": "2026-07-24",
            "observed_end": "2026-07-24",
            "recorded_at": "2026-07-24T16:00:00+00:00",
        }
    ]


def test_custom_daily_sync_persists_partial_audit_before_returning(
    tmp_path,
    monkeypatch,
):
    from app.data_providers import custom as custom_sources
    from app.data_providers.trust import load_latest_audits
    from app.services import kline_sync, preferences
    from app.tickflow.capabilities import CapabilitySet

    frame = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "date": [date(2026, 7, 24)],
            "open": [10.0],
            "high": [10.5],
            "low": [9.9],
            "close": [10.2],
            "volume": [1_000.0],
            "amount": [1_020_000.0],
        }
    )

    class Provider:
        def get_daily(self, symbols, start_time, end_time, on_chunk_done=None):
            return frame

    persisted: list[pl.DataFrame] = []
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        append_daily=persisted.append,
    )
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tushare")
    monkeypatch.setattr(
        custom_sources,
        "provider_has_dataset",
        lambda provider, dataset: provider == "tushare" and dataset == "daily",
    )
    monkeypatch.setattr(custom_sources, "get_provider", lambda provider: Provider())

    rows = kline_sync.sync_and_persist_daily_batch(
        ["600000.SH", "000001.SZ"],
        repo,
        CapabilitySet(),
        end_date=date(2026, 7, 24),
    )

    assert rows == 1
    assert persisted == [frame]
    [audit] = load_latest_audits(tmp_path)
    assert audit["provider"] == "tushare"
    assert audit["status"] == "partial"
    assert audit["missing_symbols"] == ["000001.SZ"]
    assert audit["fallback_used"] is False


def test_tickflow_daily_sync_persists_provider_audit_receipt(
    tmp_path,
    monkeypatch,
):
    from app.data_providers.trust import load_latest_audits
    from app.services import kline_sync, preferences
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

    frame = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "date": [date(2026, 7, 24)],
            "open": [10.0],
            "high": [10.5],
            "low": [9.9],
            "close": [10.2],
            "volume": [1_000.0],
            "amount": [1_020_000.0],
        }
    )
    persisted: list[pl.DataFrame] = []
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        append_daily=persisted.append,
        db=SimpleNamespace(execute=lambda statement: None),
    )
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")
    monkeypatch.setattr(kline_sync, "sync_daily_batch", lambda *args, **kwargs: frame)

    rows = kline_sync.sync_and_persist_daily_batch(
        ["600000.SH", "000001.SZ"],
        repo,
        CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(batch=10)}),
        end_date=date(2026, 7, 24),
    )

    assert rows == 1
    assert persisted == [frame]
    [audit] = load_latest_audits(tmp_path)
    assert audit["provider"] == "tickflow"
    assert audit["dataset"] == "daily"
    assert audit["status"] == "partial"
    assert audit["missing_symbols"] == ["000001.SZ"]


def test_tickflow_quote_sync_persists_provider_audit_receipt(
    tmp_path,
    monkeypatch,
):
    from app.data_providers.trust import load_latest_audits
    from app.services import kline_sync

    response = [
        {
            "symbol": "600000.SH",
            "open": 10.0,
            "high": 10.5,
            "low": 9.9,
            "last_price": 10.2,
            "volume": 1_000.0,
            "amount": 1_020_000.0,
        }
    ]
    client = SimpleNamespace(
        quotes=SimpleNamespace(get_by_universes=lambda **kwargs: response),
    )
    persisted: list[pl.DataFrame] = []
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        flush_live_daily=persisted.append,
    )
    monkeypatch.setattr(kline_sync, "get_client", lambda: client)

    rows = kline_sync.sync_daily_by_quotes(
        repo,
        requested_symbols=["600000.SH", "000001.SZ"],
    )

    assert rows == 1
    assert len(persisted) == 1
    [audit] = load_latest_audits(tmp_path)
    assert audit["provider"] == "tickflow"
    assert audit["dataset"] == "daily"
    assert audit["status"] == "partial"
    assert audit["missing_symbols"] == ["000001.SZ"]


def test_custom_daily_sync_records_but_does_not_persist_invalid_rows(
    tmp_path,
    monkeypatch,
):
    try:
        from app.data_providers.trust import DataQualityRejected
    except ImportError:
        pytest.fail("DataQualityRejected is not implemented")
    from app.data_providers import custom as custom_sources
    from app.data_providers.trust import load_latest_audits
    from app.services import kline_sync, preferences
    from app.tickflow.capabilities import CapabilitySet

    invalid = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "date": [date(2026, 7, 24)],
            "open": [10.0],
            "high": [10.1],
            "low": [9.9],
            "close": [10.2],
            "volume": [1_000.0],
            "amount": [1_020_000.0],
        }
    )

    class Provider:
        def get_daily(self, symbols, start_time, end_time, on_chunk_done=None):
            return invalid

    persisted: list[pl.DataFrame] = []
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        append_daily=persisted.append,
    )
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "akshare")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda provider, dataset: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda provider: Provider())

    with pytest.raises(DataQualityRejected, match=r"akshare.*daily.invalid_ohlc"):
        kline_sync.sync_and_persist_daily_batch(
            ["600000.SH"],
            repo,
            CapabilitySet(),
            end_date=date(2026, 7, 24),
        )

    assert persisted == []
    [audit] = load_latest_audits(tmp_path)
    assert audit["status"] == "invalid"


def test_custom_daily_sync_records_provider_errors_without_fallback(
    tmp_path,
    monkeypatch,
):
    try:
        from app.data_providers.trust import DataProviderFetchFailed
    except ImportError:
        pytest.fail("DataProviderFetchFailed is not implemented")
    from app.data_providers import custom as custom_sources
    from app.data_providers.trust import load_latest_audits
    from app.services import kline_sync, preferences
    from app.tickflow.capabilities import CapabilitySet

    class Provider:
        def get_daily(self, symbols, start_time, end_time, on_chunk_done=None):
            raise RuntimeError("upstream timeout")

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        append_daily=lambda frame: pytest.fail("failed fetch must not be persisted"),
    )
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tushare")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda provider, dataset: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda provider: Provider())

    with pytest.raises(DataProviderFetchFailed, match=r"tushare.*upstream timeout"):
        kline_sync.sync_and_persist_daily_batch(
            ["600000.SH"],
            repo,
            CapabilitySet(),
        )

    [audit] = load_latest_audits(tmp_path)
    assert audit["status"] == "error"
    assert audit["issues"] == ["daily.fetch_error:RuntimeError:upstream timeout"]
    assert audit["fallback_used"] is False


def test_new_install_defaults_daily_data_to_tushare_without_hidden_substitution(
    monkeypatch,
):
    from app.services import preferences

    monkeypatch.setattr(preferences, "load", lambda: {})

    assert preferences.get_daily_data_provider() == "tushare"
    assert preferences.get_adj_factor_provider() == "same_as_daily"


def test_selected_instrument_provider_unavailable_never_falls_back_to_tickflow(
    tmp_path,
    monkeypatch,
):
    from app.data_providers import custom as custom_sources
    from app.data_providers.trust import DataProviderUnavailable
    from app.services import instrument_sync, preferences

    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tushare")
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda provider: False)
    monkeypatch.setattr(
        instrument_sync,
        "get_client",
        lambda: pytest.fail("instrument sync must not fall back to TickFlow"),
    )

    with pytest.raises(DataProviderUnavailable, match=r"tushare.*instruments"):
        instrument_sync.sync_instruments(tmp_path)


def test_tushare_universe_uses_active_point_in_time_master_not_tickflow_pool(
    tmp_path,
    monkeypatch,
):
    from app.jobs import daily_pipeline
    from app.services import preferences
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

    instruments = tmp_path / "instruments" / "instruments.parquet"
    instruments.parent.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["600000.SH", "600001.SH"],
            "list_status": ["L", "D"],
        }
    ).write_parquet(instruments)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tushare")
    monkeypatch.setattr(daily_pipeline.settings, "data_dir", tmp_path)
    def local_pool_only(pool_id, **kwargs):
        if pool_id != "watchlist":
            pytest.fail("Tushare universe must not use TickFlow pools")
        return []

    monkeypatch.setattr(daily_pipeline, "get_pool", local_pool_only)
    capset = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(batch=100)})

    assert daily_pipeline._resolve_universe(capset) == ["600000.SH"]


def test_selected_adjustment_provider_without_dataset_never_falls_back(
    tmp_path,
    monkeypatch,
):
    from app.data_providers import custom as custom_sources
    from app.data_providers.trust import DataProviderUnavailable
    from app.services import kline_sync, preferences
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "akshare")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda provider, dataset: False)
    monkeypatch.setattr(
        kline_sync,
        "get_client",
        lambda: pytest.fail("selected factor provider must not fall back to TickFlow"),
    )
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    capset = CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits(batch=10)})

    with pytest.raises(DataProviderUnavailable, match=r"akshare.*adj_factor"):
        kline_sync.sync_adj_factor(["600000.SH"], repo, capset)


def test_custom_adjustment_sync_persists_its_own_audit_receipt(
    tmp_path,
    monkeypatch,
):
    from app.data_providers import custom as custom_sources
    from app.data_providers.trust import load_latest_audits
    from app.services import kline_sync, preferences
    from app.tickflow.capabilities import CapabilitySet

    factors = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "trade_date": [date(2026, 6, 10)],
            "ex_factor": [1.1],
        }
    )

    class Provider:
        def get_adj_factors(
            self,
            symbols,
            start_time,
            end_time,
            asset_type,
            on_chunk_done=None,
        ):
            return factors

    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "akshare")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda provider, dataset: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda provider: Provider())
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    rows, affected = kline_sync.sync_adj_factor(
        ["600000.SH"],
        repo,
        CapabilitySet(),
    )

    assert rows == 1
    assert affected == ["600000.SH"]
    audits = {audit["dataset"]: audit for audit in load_latest_audits(tmp_path)}
    assert audits["adj_factor"]["provider"] == "akshare"
    assert audits["adj_factor"]["status"] == "ok"


def test_tickflow_adjustment_sync_persists_provider_audit_receipt(
    tmp_path,
    monkeypatch,
):
    from app.data_providers.trust import load_latest_audits
    from app.services import kline_sync, preferences
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

    factors = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "trade_date": [date(2026, 6, 10)],
            "ex_factor": [1.1],
        }
    )
    client = SimpleNamespace(
        klines=SimpleNamespace(ex_factors=lambda *args, **kwargs: factors),
    )
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "tickflow")
    monkeypatch.setattr(kline_sync, "get_client", lambda: client)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    rows, affected = kline_sync.sync_adj_factor(
        ["600000.SH", "000001.SZ"],
        repo,
        CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits(batch=10)}),
        end_time=datetime(2026, 7, 24),
    )

    assert rows == 1
    assert affected == ["600000.SH"]
    [audit] = load_latest_audits(tmp_path)
    assert audit["provider"] == "tickflow"
    assert audit["dataset"] == "adj_factor"
    assert audit["status"] == "ok"
    assert audit["coverage_ratio"] == 1.0
    assert audit["missing_symbols"] == []


def test_adjustment_provider_error_is_recorded_without_fallback(
    tmp_path,
    monkeypatch,
):
    from app.data_providers import custom as custom_sources
    from app.data_providers.trust import DataProviderFetchFailed, load_latest_audits
    from app.services import kline_sync, preferences
    from app.tickflow.capabilities import CapabilitySet

    class Provider:
        def get_adj_factors(self, *args, **kwargs):
            raise RuntimeError("factor endpoint unavailable")

    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "tushare")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda provider, dataset: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda provider: Provider())
    monkeypatch.setattr(
        kline_sync,
        "get_client",
        lambda: pytest.fail("failed factor fetch must not use TickFlow"),
    )
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    with pytest.raises(DataProviderFetchFailed, match=r"tushare.*factor endpoint unavailable"):
        kline_sync.sync_adj_factor(
            ["600000.SH"],
            repo,
            CapabilitySet(),
        )

    [audit] = load_latest_audits(tmp_path)
    assert audit["dataset"] == "adj_factor"
    assert audit["status"] == "error"


def test_instrument_sync_records_provider_and_row_count(
    tmp_path,
    monkeypatch,
):
    from app.data_providers import custom as custom_sources
    from app.data_providers.trust import load_latest_audits
    from app.services import instrument_sync, preferences

    class Provider:
        def get_instruments(self, asset_type):
            return [
                {
                    "symbol": "600000.SH",
                    "name": "浦发银行",
                    "code": "600000",
                    "exchange": "SH",
                    "region": "CN",
                    "type": "stock",
                    "ext": {
                        "listing_date": "19991110",
                        "delist_date": None,
                        "list_status": "L",
                    },
                }
            ]

    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tushare")
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda provider: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda provider: Provider())

    rows = instrument_sync.sync_instruments(tmp_path)

    assert rows == 1
    audits = {audit["dataset"]: audit for audit in load_latest_audits(tmp_path)}
    assert audits["instruments"]["provider"] == "tushare"
    assert audits["instruments"]["row_count"] == 1
    assert audits["instruments"]["status"] == "ok"


def test_instrument_provider_error_is_recorded_without_tickflow_fallback(
    tmp_path,
    monkeypatch,
):
    from app.data_providers import custom as custom_sources
    from app.data_providers.trust import DataProviderFetchFailed, load_latest_audits
    from app.services import instrument_sync, preferences

    class Provider:
        def get_instruments(self, asset_type):
            raise RuntimeError("master endpoint unavailable")

    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tushare")
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda provider: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda provider: Provider())
    monkeypatch.setattr(
        instrument_sync,
        "get_client",
        lambda: pytest.fail("failed master fetch must not use TickFlow"),
    )

    with pytest.raises(DataProviderFetchFailed, match=r"tushare.*master endpoint unavailable"):
        instrument_sync.sync_instruments(tmp_path)

    [audit] = load_latest_audits(tmp_path)
    assert audit["dataset"] == "instruments"
    assert audit["status"] == "error"


def test_data_trust_api_returns_persisted_receipts(tmp_path):
    from app.api import data as data_api
    from app.data_providers.trust import audit_market_frame, write_latest_audit

    frame = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "date": [date(2026, 7, 24)],
            "open": [10.0],
            "high": [10.5],
            "low": [9.9],
            "close": [10.2],
            "volume": [1_000.0],
            "amount": [1_020_000.0],
        }
    )
    write_latest_audit(
        tmp_path,
        audit_market_frame(
            provider="tushare",
            dataset="daily",
            frame=frame,
            requested_symbols=["600000.SH"],
            requested_end=date(2026, 7, 24),
        ),
        recorded_at=datetime(2026, 7, 24, 16, 0, tzinfo=UTC),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
            )
        )
    )

    result = data_api.data_trust_status(request)

    assert result["overall_status"] == "ok"
    assert result["audits"][0]["provider"] == "tushare"
    assert result["audits"][0]["observed_end"] == "2026-07-24"
