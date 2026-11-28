import json
from types import SimpleNamespace

import pytest
from test_daily import Rules
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import MarketAdapterSpec

from tivelo_backtest.market_loading import discover_market_adapter


def adapter(**changes):
    return MarketAdapterSpec(adapter_id="fixture.rules", distribution="fixture-market", **changes)


def entry(factory):
    return SimpleNamespace(
        name="fixture.rules",
        value="fixture:build",
        group="tivelo.market_rules.v1",
        dist=SimpleNamespace(metadata={"Name": "fixture-market"}),
        load=lambda: factory,
    )


def test_discovery_does_not_import_and_load_validates_protocol(monkeypatch):
    import tivelo_backtest.market_loading as module

    calls = []

    def create(*, configuration_json):
        calls.append(configuration_json)
        return Rules()

    monkeypatch.setattr(module.metadata, "entry_points", lambda **kw: [entry(create)])
    resolved = discover_market_adapter(adapter(configuration_json='{"profile":"fixed"}'))
    assert calls == []
    assert resolved.load().reference == Rules.reference
    assert calls == ['{"profile":"fixed"}']


@pytest.mark.parametrize("count", [0, 2])
def test_missing_and_duplicate_adapters_fail_before_execution(monkeypatch, count):
    import tivelo_backtest.market_loading as module

    monkeypatch.setattr(module.metadata, "entry_points", lambda **kw: [entry(None)] * count)
    with pytest.raises(TiveloError) as error:
        discover_market_adapter(adapter())
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE


def test_wrong_distributor_and_invalid_factory_fail_safely(monkeypatch):
    import tivelo_backtest.market_loading as module

    item = entry(lambda **kw: object())
    monkeypatch.setattr(module.metadata, "entry_points", lambda **kw: [item])
    with pytest.raises(TiveloError):
        discover_market_adapter(MarketAdapterSpec(adapter_id="fixture.rules", distribution="other"))
    with pytest.raises(TiveloError) as error:
        discover_market_adapter(adapter()).load()
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE


def test_daily_loader_rejects_a_profile_that_requires_order_book_evidence(monkeypatch):
    import tivelo_backtest.market_loading as module

    item = entry(lambda **kw: SimpleNamespace(requires_order_book=True))
    monkeypatch.setattr(module.metadata, "entry_points", lambda **kw: [item])
    with pytest.raises(TiveloError) as error:
        discover_market_adapter(adapter()).load()
    assert error.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
    assert "daily data" in str(error.value)


def test_legacy_minute_loader_preserves_configuration_and_rejects_daily_profile(monkeypatch):
    import tivelo_backtest.market_loading as module

    def create(*, configuration_json):
        return lambda symbol, day, name: json.loads(configuration_json)["pct"]

    monkeypatch.setattr(module.metadata, "entry_points", lambda **kw: [entry(create)])
    selected = discover_market_adapter(adapter(configuration_json='{"pct":0.2}'))
    assert selected.load_legacy_minute_limits()("example", "day", "name") == 0.2
    monkeypatch.setattr(module.metadata, "entry_points", lambda **kw: [entry(lambda **kw: Rules())])
    with pytest.raises(TiveloError) as error:
        discover_market_adapter(adapter()).load_legacy_minute_limits()
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE


@pytest.mark.parametrize(
    "distribution", ["market[extra]", "market>=1", "https://bad", "market;python_version"]
)
def test_market_distribution_must_be_a_name(distribution):
    with pytest.raises(ValueError):
        MarketAdapterSpec(adapter_id="test", distribution=distribution)
