from decimal import Decimal, Inexact, localcontext

import pytest
from pydantic import ValidationError
from test_rules import at, instrument, rules, terms
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.market import OrderQuantityTerms


def remainder_rules(*, maximum="1000000", typed=False, schema_version="1.3"):
    term = terms(odd_lot_policy="remainder_once", sell_maximum=maximum)
    if typed:
        specific = OrderQuantityTerms(
            order_type="market",
            **{
                key: getattr(term, key)
                for key in OrderQuantityTerms.model_fields
                if key != "order_type"
            },
        )
        term = term.model_copy(update={"order_quantity_rules": (specific,)})
    return rules(term=term, schema_version=schema_version)


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize(
    "requested,available,expected",
    [
        ("99", "299", "99"),
        ("199", "299", "199"),
        ("299", "299", "299"),
        ("300", "299", "299"),
        ("298", "299", "200"),
        ("198", "299", "100"),
        ("98", "299", "0"),
        ("199.5", "299", "199"),
        ("99", "99", "99"),
        ("98", "99", "0"),
        ("299", "500", "200"),
        ("99", "500", "0"),
        ("0", "299", "0"),
        ("300", "0", "0"),
    ],
)
def test_remainder_may_be_sold_once_alone_or_with_whole_lots(typed, requested, available, expected):
    market = remainder_rules(typed=typed)
    assert market.sell_quantity(
        instrument(),
        Decimal(requested),
        available=Decimal(available),
        at=at(2),
        order_type="market" if typed else None,
    ) == Decimal(expected)


@pytest.mark.parametrize("maximum,expected", [("199", "199"), ("200", "200"), ("250", "200")])
def test_both_remainder_and_whole_lot_choices_respect_the_order_ceiling(maximum, expected):
    assert remainder_rules(maximum=maximum).sell_quantity(
        instrument(), Decimal(299), available=Decimal(299), at=at(2)
    ) == Decimal(expected)


def test_buy_lots_and_old_liquidation_policy_are_unchanged():
    assert remainder_rules().buy_quantity(instrument(), Decimal(199), at=at(2)) == 100
    previous = rules()
    assert (
        previous.sell_quantity(instrument(), Decimal(199), available=Decimal(299), at=at(2)) == 100
    )
    assert (
        previous.sell_quantity(instrument(), Decimal(299), available=Decimal(299), at=at(2)) == 299
    )


@pytest.mark.parametrize("schema_version", ["1.0", "1.1", "1.2"])
def test_new_odd_lot_policy_requires_an_explicit_configuration_boundary(schema_version):
    with pytest.raises(ValidationError, match="1.3"):
        remainder_rules(schema_version=schema_version)


def test_nonintegral_cn_holdings_are_not_treated_as_tradable_remainders():
    with pytest.raises(TiveloError) as error:
        remainder_rules().sell_quantity(
            instrument(), Decimal(299), available=Decimal("299.5"), at=at(2)
        )
    assert error.value.code == ErrorCode.CONTRACT_INVALID


def test_remainder_sizing_does_not_inherit_process_decimal_rounding():
    market = remainder_rules(maximum="10000000000000000000000000")
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        assert market.sell_quantity(
            instrument(),
            Decimal("12345678901234567890123199"),
            available=Decimal("12345678901234567890123299"),
            at=at(2),
        ) == Decimal("10000000000000000000000000")
