from app.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.services import financial_sync


def test_financial_sync_includes_quick_and_forecast_sources() -> None:
    assert "quick" in financial_sync.FINANCIAL_TABLES
    assert "forecast" in financial_sync.FINANCIAL_TABLES
    assert financial_sync._PROVIDER_TABLE_MAP["quick"] == "quick"
    assert financial_sync._PROVIDER_TABLE_MAP["forecast"] == "forecast"


def test_sync_quick_delegates_to_sync_table(tmp_path, monkeypatch) -> None:
    # sync_quick 仅经由 _sync_table 走 active provider 的本地财务表,
    # 不再读取既有 parquet / 合并外部源。
    seen: dict = {}

    def fake_sync_table(table, symbols, data_dir, capset, latest_only=True):
        seen["table"] = table
        seen["symbols"] = list(symbols)
        seen["latest_only"] = latest_only
        return 7

    monkeypatch.setattr(financial_sync, "_sync_table", fake_sync_table)
    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    assert financial_sync.sync_quick(tmp_path, capset) == 7
    assert seen["table"] == "quick"
    assert seen["latest_only"] is True


def test_sync_forecast_delegates_to_sync_table(tmp_path, monkeypatch) -> None:
    seen: dict = {}

    def fake_sync_table(table, symbols, data_dir, capset, latest_only=True):
        seen["table"] = table
        return 4

    monkeypatch.setattr(financial_sync, "_sync_table", fake_sync_table)
    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    assert financial_sync.sync_forecast(tmp_path, capset) == 4
    assert seen["table"] == "forecast"




def test_sync_all_uses_table_specific_sync_functions(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    def make_sync(table: str):
        def _sync(data_dir, capset):
            calls.append(table)
            return len(calls)
        return _sync

    monkeypatch.setattr(
        financial_sync,
        "_financial_sync_functions",
        lambda: {table: make_sync(table) for table in financial_sync.FINANCIAL_TABLES},
    )

    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    result = financial_sync.sync_all(tmp_path, capset)
    assert calls == list(financial_sync.FINANCIAL_TABLES)
    assert result["quick"] == 5
    assert result["forecast"] == 6
