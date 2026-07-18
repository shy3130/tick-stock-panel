from __future__ import annotations

from app.data_providers import custom as custom_sources
from app.services import financial_sync, preferences
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.policy import _augment_custom_sources


def test_custom_financial_source_grants_financial_capability(monkeypatch) -> None:
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "clickhouse")
    monkeypatch.setattr(
        custom_sources,
        "provider_has_dataset",
        lambda provider, dataset: provider == "clickhouse" and dataset == "financial",
    )
    capset = CapabilitySet()

    _augment_custom_sources(capset)

    assert capset.has(Cap.FINANCIAL)


def test_sync_all_allows_custom_financial_source_without_tickflow_capability(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: True)
    monkeypatch.setattr(financial_sync, "_get_symbols", lambda _data_dir: ["700.HK"])
    synced: list[str] = []

    def sync_table(table, symbols, data_dir, capset, latest_only=True):
        synced.append(table)
        return 1

    monkeypatch.setattr(financial_sync, "_sync_table", sync_table)

    result = financial_sync.sync_all(tmp_path, CapabilitySet())

    assert result == {
        "metrics": 1,
        "income": 1,
        "balance_sheet": 1,
        "cash_flow": 1,
    }
    assert synced == list(financial_sync.FINANCIAL_TABLES)


def test_scheduler_restores_sync_times_for_custom_financial_source(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: True)
    monkeypatch.setattr(
        preferences,
        "get_financial_sync_times",
        lambda: {"metrics": "2026-07-19T01:02:03+00:00"},
    )
    scheduler = financial_sync.FinancialScheduler()

    scheduler.start(tmp_path, CapabilitySet())

    assert scheduler.last_sync == {"metrics": "2026-07-19T01:02:03+00:00"}
