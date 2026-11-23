from importlib import metadata

import pytest
from test_rules import rules

from tivelo_market_cn.adapter import CNMarketProfile, create_rules


def test_installed_adapter_uses_exact_calendar_and_rule_configuration():
    original = rules()
    profile = CNMarketProfile(
        configuration=original.configuration, calendar=original.calendar.version
    )
    entry = [
        item
        for item in metadata.entry_points(group="tivelo.market_rules.v1")
        if item.name == "tivelo.market.cn.cash-equity"
    ]
    assert len(entry) == 1
    restored = entry[0].load()(configuration_json=profile.model_dump_json())
    assert restored.reference == original.reference
    assert restored.configuration == original.configuration
    assert restored.calendar.version == original.calendar.version


def test_adapter_does_not_infer_missing_calendar_or_fees():
    with pytest.raises(ValueError):
        create_rules(configuration_json="{}")
    original = rules()
    profile = CNMarketProfile(
        configuration=original.configuration, calendar=original.calendar.version
    )
    values = profile.model_dump()
    values["calendar"]["contents"]["days"][0]["is_open"] = False
    with pytest.raises(ValueError):
        create_rules(configuration_json=__import__("json").dumps(values, default=str))
