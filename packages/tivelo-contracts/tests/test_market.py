from decimal import Decimal, Inexact, localcontext

import pytest
from pydantic import ValidationError

from tivelo_contracts.execution import LedgerEntry, OrderEvent
from tivelo_contracts.market import (
    FeeComponent,
    FeeQuote,
    OrderQuantityTerms,
    PriceBounds,
    ReferencePrice,
    TradingTerms,
)


def term(**changes):
    values = dict(
        spec_revision="fixture",
        market_id="cn.equity",
        venue_id="XSHG",
        asset_class="equity",
        instrument_type="common_stock",
        quantity_unit="share",
        currency="CNY",
        price_tick="0.01",
        buy_minimum="100",
        buy_increment="100",
        buy_maximum="1000000",
        sell_minimum="100",
        sell_increment="100",
        sell_maximum="1000000",
        odd_lot_policy="all_remaining",
        sell_lag_sessions=1,
        price_limit_mode="relative_reference",
        limit_up_ratio="0.1",
        limit_down_ratio="0.1",
        source_reference="synthetic",
        valid_from="2025-01-01T00:00:00Z",
        available_at="2024-12-01T00:00:00Z",
        revision_id="1",
        source_id="synthetic",
    )
    values.update(changes)
    return TradingTerms(**values)


def component(fee_id="commission", amount="5"):
    return FeeComponent(
        fee_id=fee_id,
        amount=amount,
        rate="0",
        minimum_applied=True,
        rule_revision="1",
        source_reference="synthetic",
    )


def order_quantities(order_type="market", **changes):
    values = dict(
        order_type=order_type,
        buy_minimum="200",
        buy_increment="1",
        buy_maximum="50000",
        sell_minimum="200",
        sell_increment="1",
        sell_maximum="50000",
        odd_lot_policy="all_remaining",
    )
    return OrderQuantityTerms(**{**values, **changes})


def test_optional_order_quantity_rules_preserve_old_wire_and_are_lossless():
    original = term()
    assert "order_quantity_rules" not in original.model_dump()
    specific = term(order_quantity_rules=(order_quantities(),))
    assert TradingTerms.model_validate_json(specific.model_dump_json()) == specific
    assert specific.order_quantity_rules[0].buy_increment == 1


@pytest.mark.parametrize("factory", [term, order_quantities])
def test_remainder_policy_is_explicit_and_requires_a_whole_lot_grid(factory):
    original = factory(odd_lot_policy="remainder_once", sell_minimum="100", sell_increment="100")
    assert type(original).model_validate_json(original.model_dump_json()) == original
    with pytest.raises(ValidationError, match="remainder_once"):
        factory(odd_lot_policy="remainder_once", sell_minimum="200", sell_increment="1")


@pytest.mark.parametrize("types", [("market", "market"), ("market", "limit")])
def test_ambiguous_or_unordered_type_rules_are_rejected(types):
    with pytest.raises(ValidationError):
        term(order_quantity_rules=tuple(order_quantities(t) for t in types))


@pytest.mark.parametrize(
    "changes",
    [
        dict(order_type="stop"),
        dict(buy_maximum="199"),
        dict(sell_maximum="199"),
        dict(buy_increment="0"),
        dict(sell_increment=0.5),
    ],
)
def test_invalid_type_specific_quantities_are_rejected(changes):
    with pytest.raises(ValidationError):
        order_quantities(**changes)


def test_market_facts_roundtrip_without_binary_float_conversion():
    terms = term()
    assert TradingTerms.model_validate_json(terms.model_dump_json()) == terms
    reference = ReferencePrice(
        instrument_id="A",
        trading_date="2025-01-02",
        value="10.05",
        kind="exchange_ex_rights",
        available_at="2025-01-01T12:00:00Z",
        source_id="synthetic",
        revision_id="1",
    )
    assert ReferencePrice.model_validate_json(reference.model_dump_json()) == reference
    assert terms.available_at < terms.valid_from


@pytest.mark.parametrize(
    "changes",
    [
        dict(buy_maximum="99"),
        dict(sell_maximum="99"),
        dict(buy_increment="0"),
        dict(price_tick=0.01),
        dict(sell_lag_sessions=True),
        dict(limit_down_ratio="1.01"),
        dict(limit_up_ratio="0"),
        dict(limit_down_ratio=None),
        dict(price_limit_mode="unknown"),
    ],
)
def test_incomplete_or_inconsistent_terms_are_not_defaulted(changes):
    with pytest.raises(ValidationError):
        term(**changes)


@pytest.mark.parametrize(
    "values",
    [
        dict(state="limited", lower="9", upper="11"),
        dict(state="limited", lower="11", upper="9", reference_revision="p1"),
        dict(state="unlimited", lower="9"),
        dict(state="unlimited", reference_revision="p1"),
    ],
)
def test_price_bounds_require_an_explicit_consistent_state(values):
    with pytest.raises(ValidationError):
        PriceBounds(rule_revision="1", **values)


def test_fee_sum_is_exact_and_duplicate_charges_cannot_be_hidden():
    items = (component(amount="12345.67"), component("transfer", "0.01"))
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        quote = FeeQuote(currency="CNY", notional="100000", components=items, total="12345.68")
    assert quote.total == Decimal("12345.68")
    for components, total in ((items, "12345.67"), ((items[0], items[0]), "24691.34")):
        with pytest.raises(ValidationError):
            FeeQuote(currency="CNY", notional="100000", components=components, total=total)


def test_order_event_retains_fee_details_and_chronology():
    values = dict(
        order_id="o1",
        instrument_id="A",
        side="buy",
        status="filled",
        decision_at="2025-01-02T07:00:00Z",
        submitted_at="2025-01-02T07:00:00Z",
        eligible_at="2025-01-03T01:30:00Z",
        filled_at="2025-01-03T01:30:00Z",
        event_at="2025-01-03T01:30:00Z",
        requested_quantity="100",
        filled_quantity="100",
        raw_fill_price="10",
        fee="5",
        fee_components=(component(),),
        currency="CNY",
    )
    order = OrderEvent(**values)
    assert "order_type" not in order.model_dump()
    assert "raw_limit_price" not in order.model_dump()
    assert OrderEvent.model_validate_json(order.model_dump_json()) == order
    for change in (dict(fee="4"), dict(event_at="2025-01-02T07:00:00Z")):
        with pytest.raises(ValidationError):
            OrderEvent(**{**values, **change})
    limit_order = OrderEvent(**values, order_type="limit", raw_limit_price="10")
    assert OrderEvent.model_validate_json(limit_order.model_dump_json()) == limit_order
    # The generic event format does not impose cash-equity positivity on future assets.
    negative_price = OrderEvent(
        **{
            **values,
            "raw_fill_price": "-40",
            "order_type": "limit",
            "raw_limit_price": "-39",
        }
    )
    assert negative_price.raw_limit_price == -39
    for change in (
        dict(order_type="limit"),
        dict(raw_limit_price="10"),
        dict(order_type="market", raw_limit_price="10"),
        dict(order_type="limit", raw_limit_price="9.99"),
        dict(side="sell", order_type="limit", raw_limit_price="10.01"),
    ):
        with pytest.raises(ValidationError):
            OrderEvent(**{**values, **change})


@pytest.mark.parametrize(
    "kind,cash,quantity,receivable",
    [
        ("buy", "100", "10", "0"),
        ("buy", "-100", "10", "5"),
        ("sell", "100", "10", "0"),
        ("split", "1", "10", "0"),
        ("dividend_accrual", "1", "0", "10"),
        ("dividend_accrual", "0", "0", "-10"),
        ("dividend_payment", "10", "0", "0"),
        ("dividend_payment", "10", "1", "-10"),
    ],
)
def test_ledger_rejects_misclassified_cash_quantity_and_receivables(
    kind, cash, quantity, receivable
):
    with pytest.raises(ValidationError):
        LedgerEntry(
            entry_id="1",
            at="2025-01-03T01:30:00Z",
            kind=kind,
            reference_id="event",
            instrument_id="A",
            currency="CNY",
            cash_delta=cash,
            quantity_delta=quantity,
            receivable_delta=receivable,
        )


def test_payment_conservation_is_independent_of_decimal_context():
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        entry = LedgerEntry(
            entry_id="1",
            at="2025-01-03T01:30:00Z",
            kind="dividend_payment",
            reference_id="event",
            instrument_id="A",
            currency="CNY",
            cash_delta="12345.6789",
            quantity_delta="0",
            receivable_delta="-12345.6789",
        )
    assert entry.receivable_delta == Decimal("-12345.6789")
