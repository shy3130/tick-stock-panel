"""能力标准统一: 自定义/插件数据源能力增广回归测试。

对应 _augment_custom_sources 的数据集→能力映射
(daily/adj_factor/minute/depth5/financial/full_minute):
某数据集的当前 provider 非 tickflow 且声明了该数据集 → grant 对应能力;
取数路由仍按 preferences 分流, 不会误调 TickFlow。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.policy import _augment_custom_sources


def _set_providers(monkeypatch, *, daily="tickflow", adj="tickflow",
                   minute="tickflow", depth5="tickflow", financial="tickflow",
                   full_minute="tickflow") -> None:
    """mock preferences 各数据集 provider getter。"""
    from app.services import preferences
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: daily)
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: adj)
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: minute)
    monkeypatch.setattr(preferences, "get_depth5_data_provider", lambda: depth5)
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: financial)
    monkeypatch.setattr(preferences, "get_full_minute_data_provider", lambda: full_minute)


def _set_datasets(monkeypatch, datasets: set[str]) -> None:
    """mock provider_has_dataset: 非 tickflow provider 对给定数据集返回 True。"""
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset",
        lambda name, ds: name != "tickflow" and ds in datasets,
    )


def test_daily_custom_source_grants_daily_batch(monkeypatch):
    _set_providers(monkeypatch, daily="mock_src")
    _set_datasets(monkeypatch, {"daily"})
    capset = CapabilitySet()
    _augment_custom_sources(capset)
    assert capset.has(Cap.KLINE_DAILY_BATCH)
    # 未声明其他数据集 → 不补
    assert not capset.has(Cap.ADJ_FACTOR)
    assert not capset.has(Cap.KLINE_MINUTE_BATCH)
    assert not capset.has(Cap.FINANCIAL)


def test_adj_custom_source_grants_adj_factor(monkeypatch):
    """adj 显式路由到声明除权的自定义源 → 补授能力 (跟随日K已下线, 独立判定)。"""
    _set_providers(monkeypatch, adj="mock_src")
    _set_datasets(monkeypatch, {"adj_factor"})
    capset = CapabilitySet()
    _augment_custom_sources(capset)
    assert capset.has(Cap.ADJ_FACTOR)


def test_full_minute_custom_source_grants_intraday_universe(monkeypatch):
    """全量分钟: 声明 full_minute 数据集且被路由 → 补授 INTRADAY_UNIVERSE,
    minute_refresh 服务门控与 TickFlow Expert 口径统一。"""
    _set_providers(monkeypatch, full_minute="mock_src")
    _set_datasets(monkeypatch, {"full_minute"})
    capset = CapabilitySet()
    _augment_custom_sources(capset)
    assert capset.has(Cap.INTRADAY_UNIVERSE)
    # 声明了 full_minute 不等于声明 minute → 不补逐标的分钟K能力
    assert not capset.has(Cap.KLINE_MINUTE_BATCH)


def test_full_minute_dataset_without_routing_not_granted(monkeypatch):
    """源声明了 full_minute 但路由仍是 tickflow → 不增广 (TickFlow 档位自决)。"""
    _set_providers(monkeypatch, full_minute="tickflow")
    _set_datasets(monkeypatch, {"full_minute"})
    capset = CapabilitySet()
    _augment_custom_sources(capset)
    assert not capset.has(Cap.INTRADAY_UNIVERSE)


def test_minute_custom_source_grants_minute_batch(monkeypatch):
    """原有 minute 增广行为保持。"""
    _set_providers(monkeypatch, minute="mock_src")
    _set_datasets(monkeypatch, {"minute"})
    capset = CapabilitySet()
    _augment_custom_sources(capset)
    assert capset.has(Cap.KLINE_MINUTE_BATCH)


def test_depth5_custom_source_grants_depth_batch(monkeypatch):
    """五档独立路由到声明 depth5 的自定义源时补授批量五档能力。"""
    _set_providers(monkeypatch, depth5="mock_src")
    _set_datasets(monkeypatch, {"depth5"})
    capset = CapabilitySet()
    _augment_custom_sources(capset)
    assert capset.has(Cap.DEPTH5_BATCH)


def test_financial_custom_source_grants_financial(monkeypatch):
    _set_providers(monkeypatch, financial="mock_src")
    _set_datasets(monkeypatch, {"financial"})
    capset = CapabilitySet()
    _augment_custom_sources(capset)
    assert capset.has(Cap.FINANCIAL)


def test_provider_active_but_dataset_not_declared_no_grant(monkeypatch):
    """provider 被选为当前源但未声明该数据集 → 不 grant (回退 TickFlow 语义)。"""
    _set_providers(monkeypatch, daily="mock_src", minute="mock_src",
                   adj="mock_src", financial="mock_src")
    _set_datasets(monkeypatch, set())  # 什么都不声明
    capset = CapabilitySet()
    _augment_custom_sources(capset)
    assert not capset.has(Cap.KLINE_DAILY_BATCH)
    assert not capset.has(Cap.ADJ_FACTOR)
    assert not capset.has(Cap.KLINE_MINUTE_BATCH)
    assert not capset.has(Cap.FINANCIAL)


def test_tickflow_active_no_grant(monkeypatch):
    """全部数据集仍走 tickflow → 不补任何能力。"""
    _set_providers(monkeypatch)  # 默认全 tickflow
    _set_datasets(monkeypatch, {"daily", "adj_factor", "minute", "financial"})
    capset = CapabilitySet()
    _augment_custom_sources(capset)
    assert not capset.has(Cap.KLINE_DAILY_BATCH)
    assert not capset.has(Cap.ADJ_FACTOR)
    assert not capset.has(Cap.KLINE_MINUTE_BATCH)
    assert not capset.has(Cap.FINANCIAL)


def test_grant_does_not_override_tickflow_limits(monkeypatch):
    """grant 不覆盖 TickFlow 已有能力及其限制。"""
    _set_providers(monkeypatch, minute="mock_src")
    _set_datasets(monkeypatch, {"minute"})
    capset = CapabilitySet()
    capset.grant(Cap.KLINE_MINUTE_BATCH, CapabilityLimits(rpm=30, batch=100))
    _augment_custom_sources(capset)
    lim = capset.limits(Cap.KLINE_MINUTE_BATCH)
    assert lim is not None and lim.rpm == 30 and lim.batch == 100


def test_update_data_providers_refreshes_capability_snapshot(monkeypatch):
    """切换数据源后 app.state.capabilities 快照应刷新 (读缓存+增广, 无网络)。"""
    from app.api import settings as settings_api

    monkeypatch.setattr("app.services.preferences.save", lambda upd: None)
    sentinel = CapabilitySet()
    monkeypatch.setattr(settings_api, "detect_capabilities", lambda: sentinel)

    mock_request = MagicMock()
    settings_api.update_data_providers(
        MagicMock(model_dump=lambda exclude_none: {"daily_data_provider": "mock_src"}),
        mock_request,
    )
    assert mock_request.app.state.capabilities is sentinel
