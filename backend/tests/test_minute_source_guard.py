"""历史分钟同步只因当前分钟源发生变化而停止。"""
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app import secrets_store
from app.config import settings
from app.data_providers.custom import loader
from app.data_providers.custom.config import AuthConfig, CustomSourceConfig, DatasetConfig
from app.services import kline_sync, minute_shared_pull, preferences
from app.services.shared_work import WorkBusyError
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    preferences._invalidate_cache()
    monkeypatch.setattr(secrets_store, "_cache", None)
    monkeypatch.setattr(secrets_store, "_cache_sig", None)
    yield
    preferences._invalidate_cache()


@pytest.mark.parametrize("change", ["unrelated", "route", "key", "endpoint", "basis"])
def test_backward_sync_checks_only_active_source_before_each_write(tmp_path, monkeypatch, change):
    repo = KlineRepository(DataStore(tmp_path))
    caps = CapabilitySet()
    caps.grant(Cap.KLINE_MINUTE_BATCH)
    symbol = "600000.SH"
    first = datetime(2026, 11, 2, 9, 31)
    second = first.replace(minute=32)
    secrets_store.save({"tickflow_api_key": "fixture-key"})

    def fetch(_symbols, *, on_segment, **_):
        def persist(dt):
            on_segment(pl.DataFrame({"symbol": [symbol], "datetime": [dt], "close": [1.0]}))

        persist(first)
        if change == "unrelated":
            preferences.set_ui_prefs({"theme": "dark"})
            preferences.set_financial_sync_time("metrics", "2026-11-02T20:00:00")
            preferences.save({"daily_data_provider": "other", "minute_sync_days": 10})
            secrets_store.save({"ai_model": "fixture", "tickflow_api_key": "fixture-key"})
        elif change == "route":
            preferences.save({"minute_data_provider": "other"})
        elif change == "key":
            secrets_store.save({"tickflow_api_key": "fixture-key-2"})
        elif change == "endpoint":
            secrets_store.save({"tickflow_base_url": "https://other.example"})
        else:
            kline_sync.minute_adjust.mark_minute_basis_raw(tmp_path)
        persist(second)

    monkeypatch.setattr(kline_sync, "sync_minute_batch", fetch)

    def sync():
        return kline_sync.sync_and_persist_minute([symbol], repo, caps, extend_backward=True)

    if change == "unrelated":
        assert sync() > 0
        expected = [first, second]
    else:
        with pytest.raises(WorkBusyError, match="价格基准" if change == "basis" else "分钟数据源配置已变化"):
            sync()
        expected = [first]
    assert repo.get_minute(symbol, first.date())["datetime"].to_list() == expected


def test_plugin_stamp_tracks_selected_key_and_running_version(monkeypatch):
    name = "fixture_plugin"
    status = {"api_key_env": "FIXTURE_MINUTE_KEY", "version": "1.0", "available": True, "datasets": ["minute"]}
    monkeypatch.setitem(loader._PLUGIN_STATUS, name, status)
    monkeypatch.setitem(loader._PROVIDERS, name, SimpleNamespace())
    monkeypatch.setenv("FIXTURE_MINUTE_KEY", "fixture-env-value")
    preferences.save({"minute_data_provider": name})
    first = kline_sync.minute_source_stamp()
    secrets_store.save({"tickflow_api_key": "other", "ai_model": "other"})
    assert kline_sync.minute_source_stamp() == first
    secrets_store.save({f"{name}_api_key": "fixture-local-value"})
    second = kline_sync.minute_source_stamp()
    assert second != first and "fixture-local-value" not in repr(second)
    status["version"] = "2.0"
    assert kline_sync.minute_source_stamp() != second


def test_yaml_stamp_tracks_minute_mapping_and_effective_auth(monkeypatch):
    name = "fixture_yaml"
    minute = DatasetConfig(url="https://minute.example", field_map={"close": "price"})
    config = CustomSourceConfig(name, name, AuthConfig("bearer", "FIXTURE_MINUTE_KEY"), {"minute": minute})
    monkeypatch.setitem(loader._PROVIDERS, name, SimpleNamespace(config=config))
    monkeypatch.setenv("FIXTURE_MINUTE_KEY", "fixture-first")
    preferences.save({"minute_data_provider": name})
    first = kline_sync.minute_source_stamp()
    config.datasets["daily"] = DatasetConfig(url="https://daily.example")
    assert kline_sync.minute_source_stamp() == first
    minute.field_map["close"] = "last"
    second = kline_sync.minute_source_stamp()
    assert second != first
    monkeypatch.setenv("FIXTURE_MINUTE_KEY", "fixture-second")
    assert kline_sync.minute_source_stamp() != second


def test_batch_waiting_for_write_lock_rechecks_source(tmp_path, monkeypatch):
    symbol = "600000.SH"

    @contextmanager
    def lock():
        # 模拟拉取已返回、等待另一个写入释放仓库锁期间发生切源。
        preferences.save({"minute_data_provider": "other"})
        yield

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), _write_lock=lock())
    monkeypatch.setattr(kline_sync, "sync_minute_batch", lambda *_args, **_kw: pl.DataFrame({"symbol": [symbol]}))
    with pytest.raises(WorkBusyError, match="分钟数据源或价格基准已变化"):
        minute_shared_pull.pull(
            repo, [symbol], asset_type="stock", start=datetime(2026, 11, 2, 9, 30),
            end=datetime(2026, 11, 2, 15), batch_size=1, rpm=60, raw_basis=False,
        )
    assert not list((tmp_path / "kline_minute").rglob("*.parquet"))


def test_mixed_assets_cannot_restart_with_new_source_between_groups(tmp_path, monkeypatch):
    repo = KlineRepository(DataStore(tmp_path))
    caps = CapabilitySet()
    caps.grant(Cap.KLINE_MINUTE_BATCH)
    stock, etf = "600000.SH", "510300.SH"
    monkeypatch.setattr(repo, "get_etf_symbol_set", lambda: {etf})
    fetched = []

    def fetch(symbols, *, on_segment, **_):
        fetched.extend(symbols)
        on_segment(pl.DataFrame({"symbol": symbols, "datetime": datetime(2026, 11, 2, 9, 31), "close": 1.0}))
        secrets_store.save({"tickflow_api_key": "fixture-replacement"})

    monkeypatch.setattr(kline_sync, "sync_minute_batch", fetch)
    with pytest.raises(WorkBusyError, match="分钟数据源配置已变化"):
        kline_sync.sync_and_persist_minute([stock, etf], repo, caps, extend_backward=True)
    assert fetched == [stock]
    assert repo.get_minute(stock, datetime(2026, 11, 2).date()).height == 1
    assert not list((tmp_path / "kline_etf_minute").rglob("*.parquet"))
