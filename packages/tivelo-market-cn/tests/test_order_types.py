from decimal import Decimal

import pytest
from test_rules import at, instrument, rules, terms
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.market import OrderQuantityTerms
from tivelo_contracts.protocols import OrderExecutionRules


def quantities(order_type, **changes):
    values = dict(
        order_type=order_type,
        buy_minimum="200",
        buy_increment="1",
        buy_maximum="50000" if order_type == "market" else "100000",
        sell_minimum="200",
        sell_increment="1",
        sell_maximum="50000" if order_type == "market" else "100000",
        odd_lot_policy="all_remaining",
    )
    return OrderQuantityTerms(**{**values, **changes})


def typed_rules(*order_types):
    return rules(
        term=terms(order_quantity_rules=tuple(quantities(t) for t in order_types)),
        schema_version="1.1",
    )


@pytest.mark.parametrize(
    "order_type,quantity,expected",
    [
        ("market", "60000", "50000"),
        ("limit", "60000", "60000"),
        ("limit", "100001", "100000"),
        ("market", "199", "0"),
        ("limit", "201", "201"),
    ],
)
def test_each_order_type_has_its_own_minimum_increment_and_ceiling(order_type, quantity, expected):
    market = typed_rules("limit", "market")
    assert isinstance(market, OrderExecutionRules)
    assert market.order_quantity(
        instrument(), "buy", Decimal(quantity), order_type=order_type, at=at(2)
    ) == Decimal(expected)


@pytest.mark.parametrize(
    "order_type,requested,available,expected",
    [
        ("market", "60000", "60000", "50000"),
        ("limit", "60000", "60000", "60000"),
        ("market", "199", "199", "199"),
        ("limit", "150", "199", "0"),
        ("limit", "100000", "100123", "100000"),
    ],
)
def test_sell_type_ceiling_does_not_disappear_for_odd_lots(
    order_type, requested, available, expected
):
    assert typed_rules("limit", "market").order_quantity(
        instrument(),
        "sell",
        Decimal(requested),
        available=Decimal(available),
        order_type=order_type,
        at=at(2),
    ) == Decimal(expected)


def test_old_quantity_entry_cannot_silently_use_generic_values_for_typed_terms():
    market = typed_rules("limit", "market")
    with pytest.raises(TiveloError) as error:
        market.buy_quantity(instrument(), Decimal(60000), at=at(2))
    assert error.value.code == ErrorCode.CONTRACT_INVALID
    with pytest.raises(TiveloError):
        market.sell_quantity(instrument(), Decimal(60000), available=Decimal(60000), at=at(2))
    assert market.buy_quantity(instrument(), Decimal(60000), order_type="market", at=at(2)) == 50000


def test_limit_only_profile_and_unknown_type_fail_without_fallback():
    market = typed_rules("limit")
    with pytest.raises(TiveloError) as error:
        market.quantity_terms(instrument(), "market", at=at(2))
    assert error.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
    with pytest.raises(ValueError):
        market.quantity_terms(instrument(), "iceberg", at=at(2))
    with pytest.raises(TiveloError):
        market.order_quantity(instrument(), "sell", Decimal(200), order_type="limit", at=at(2))


def test_generic_profile_retains_its_original_quantities_for_both_explicit_types():
    market = rules()
    for order_type in ("limit", "market"):
        assert (
            market.order_quantity(
                instrument(), "buy", Decimal(299), order_type=order_type, at=at(2)
            )
            == market.buy_quantity(instrument(), Decimal(299), at=at(2))
            == 200
        )


def test_type_specific_terms_are_historical_and_whole_share_rules():
    market = typed_rules("limit", "market")
    with pytest.raises(TiveloError) as error:
        market.quantity_terms(instrument(available_at=at(3)), "market", at=at(2))
    assert error.value.code == ErrorCode.FUTURE_INFORMATION
    with pytest.raises(TiveloError) as error:
        rules(
            term=terms(order_quantity_rules=(quantities("market", buy_increment="0.5"),)),
            schema_version="1.1",
        )
    assert error.value.code == ErrorCode.CONTRACT_INVALID


def test_installed_factory_preserves_typed_configuration_and_fingerprint():
    from importlib.metadata import entry_points

    from tivelo_market_cn.adapter import CNMarketProfile

    market = typed_rules("limit", "market")
    profile = CNMarketProfile(configuration=market.configuration, calendar=market.calendar.version)
    (entry,) = entry_points(group="tivelo.market_rules.v1", name="tivelo.market.cn.cash-equity")
    restored = entry.load()(configuration_json=profile.model_dump_json())
    assert restored.reference == market.reference
    assert (
        restored.order_quantity(instrument(), "buy", Decimal(60000), order_type="market", at=at(2))
        == 50000
    )


def test_new_constraints_cannot_be_disguised_as_an_old_configuration():
    with pytest.raises(ValueError, match="schema 1.1"):
        rules(term=terms(order_quantity_rules=(quantities("market"),)))
    market = typed_rules("market")
    assert market.configuration.schema_version == "1.1"
    from tivelo_market_cn.adapter import CNMarketProfile, create_rules

    profile = CNMarketProfile(configuration=market.configuration, calendar=market.calendar.version)
    invalid = profile.model_dump_json().replace('"schema_version":"1.1"', '"schema_version":"1.0"')
    with pytest.raises(ValueError, match="schema 1.1"):
        create_rules(configuration_json=invalid)
