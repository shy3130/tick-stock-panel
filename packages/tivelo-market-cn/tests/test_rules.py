from datetime import UTC, date, datetime
from decimal import Decimal, Inexact, localcontext

import pytest
from pydantic import ValidationError
from tivelo_contracts.data import Instrument
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.market import ReferencePrice, TradingTerms
from tivelo_contracts.protocols import ExecutionRules, MarketRules

from tivelo_market_cn.calendar import (
    CalendarContents,
    CalendarDay,
    CalendarVersion,
    CNCalendar,
    SessionInterval,
)
from tivelo_market_cn.rules import CNTradingRules, FeeRule, RuleConfiguration


def at(day, hour=1):
    return datetime(2025, 1, day, hour, tzinfo=UTC)


def calendar():
    return CNCalendar(
        CalendarVersion.create(
            CalendarContents(
                calendar_id="synthetic-cn",
                venue_id="XSHG",
                source_id="synthetic",
                source_sha256="a" * 64,
                available_at=at(1, 0),
                bar_convention_id="fixture.daily",
                days=tuple(
                    CalendarDay(
                        trading_date=date(2025, 1, day),
                        is_open=day in (2, 3, 6),
                        sessions=(
                            SessionInterval(
                                session_id=f"s{day}",
                                kind="continuous",
                                start=at(day, 1),
                                end=at(day, 7),
                            ),
                        )
                        if day in (2, 3, 6)
                        else (),
                        closed_reason=None if day in (2, 3, 6) else "synthetic-closed",
                    )
                    for day in range(1, 7)
                ),
            )
        )
    )


def instrument(**changes):
    values = dict(
        instrument_id="security-1",
        venue_id="XSHG",
        market_id="cn.equity",
        asset_class="equity",
        instrument_type="common_stock",
        display_symbol="600001.SH",
        name="synthetic",
        quote_currency="CNY",
        settlement_currency="CNY",
        calendar_id="synthetic-cn",
        timezone="Asia/Shanghai",
        listing_date=dict(state="known", value="2024-01-01"),
        delisting_date=dict(state="not_yet"),
        tradable=True,
        spec_revision="main-v1",
        valid_from=at(1, 0),
        available_at=at(1, 0),
        revision_id="i1",
        source_id="synthetic",
    )
    values.update(changes)
    return Instrument(**values)


def terms(**changes):
    values = dict(
        spec_revision="main-v1",
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
        source_reference="synthetic-only",
        valid_from=at(1, 0),
        available_at=at(1, 0),
        revision_id="r1",
        source_id="synthetic",
    )
    values.update(changes)
    return TradingTerms(**values)


def fee(fee_id="commission", **changes):
    values = dict(
        fee_id=fee_id,
        side="both",
        rate="0.0002",
        minimum="5",
        quantum="0.01",
        rounding="half_up",
        source_reference="synthetic-account-contract",
        included_fee_ids=(),
        valid_from=at(1, 0),
        available_at=at(1, 0),
        revision_id=f"{fee_id}-v1",
        source_id="synthetic",
    )
    values.update(changes)
    return FeeRule(**values)


def rules(*, term=None, fees=None, required=None, schema_version="1.0"):
    fees = (fee(), fee("stamp", side="sell", rate="0.0005", minimum="0")) if fees is None else fees
    return CNTradingRules(
        RuleConfiguration(
            schema_version=schema_version,
            profile_id="cn.fixture",
            profile_version="1",
            currency="CNY",
            terms=(term or terms(),),
            required_fee_ids=required or tuple(sorted({f.fee_id for f in fees})),
            fee_rules=fees,
        ),
        calendar(),
    )


@pytest.mark.parametrize("amount,expected", [(99, 0), (100, 100), (299, 200), (1000001, 1000000)])
def test_buy_quantization_and_order_ceiling(amount, expected):
    assert rules().buy_quantity(instrument(), Decimal(amount), at=at(2)) == expected


def test_minimum_is_separate_from_increment():
    book = rules(term=terms(buy_minimum="200", buy_increment="1", buy_maximum="50000"))
    assert book.buy_quantity(instrument(), Decimal(199), at=at(2)) == 0
    assert book.buy_quantity(instrument(), Decimal(201), at=at(2)) == 201
    assert book.buy_quantity(instrument(), Decimal(50001), at=at(2)) == 50000


@pytest.mark.parametrize(
    "requested,available,expected", [(50, 55, 0), (55, 55, 55), (255, 255, 255), (155, 255, 100)]
)
def test_remaining_odd_lot_is_explicit(requested, available, expected):
    assert (
        rules().sell_quantity(
            instrument(), Decimal(requested), available=Decimal(available), at=at(2)
        )
        == expected
    )


def test_t_plus_one_skips_explicit_closed_dates():
    assert rules().sellable_at(instrument(), at(3, 2)) == at(6)
    zero = rules(term=terms(sell_lag_sessions=0))
    assert zero.sellable_at(instrument(), at(3, 2)) == at(3, 2)
    with pytest.raises(TiveloError):
        rules().sellable_at(instrument(), at(4, 2))


def test_last_calendar_day_does_not_guess_next_weekday():
    with pytest.raises(TiveloError) as error:
        rules().sellable_at(instrument(), at(6, 2))
    assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT


def test_fee_components_exact_minimum_sell_tax_and_effective_boundary():
    book = rules(
        fees=(
            fee(),
            fee("stamp", side="sell", rate="0.001", minimum="0", valid_to=at(3, 0)),
            fee(
                "stamp",
                side="sell",
                rate="0.0005",
                minimum="0",
                valid_from=at(3, 0),
                revision_id="stamp-v2",
            ),
        )
    )
    assert book.fees(instrument(), "buy", Decimal(100), Decimal(10), at=at(2)) == Decimal(5)
    old = book.fee_breakdown(instrument(), "sell", Decimal(1000), Decimal(10), at=at(2))
    new = book.fee_breakdown(instrument(), "sell", Decimal(1000), Decimal(10), at=at(3))
    assert [(f.fee_id, f.amount, f.minimum_applied) for f in old.components] == [
        ("commission", Decimal(5), True),
        ("stamp", Decimal(10), False),
    ]
    assert (old.total, new.total) == (Decimal(15), Decimal(10))


def test_rounding_is_per_component_and_does_not_inherit_decimal_context():
    book = rules(
        fees=(fee(rate="0.0005", minimum="0"), fee("transfer", rate="0.0005", minimum="0"))
    )
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        quote = book.fee_breakdown(instrument(), "buy", Decimal(1), Decimal(10), at=at(2))
    assert quote.total == Decimal("0.02")
    assert [part.amount for part in quote.components] == [Decimal("0.01"), Decimal("0.01")]


@pytest.mark.parametrize(
    "fees,required",
    [
        ((fee(valid_to=at(2, 0)),), ("commission",)),
        ((fee(available_at=at(3)),), ("commission",)),
        ((fee(),), ("commission", "transfer")),
    ],
)
def test_missing_or_unknown_fee_cannot_become_zero(fees, required):
    with pytest.raises(TiveloError) as error:
        rules(fees=fees, required=required).fees(
            instrument(), "buy", Decimal(100), Decimal(10), at=at(2)
        )
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE


def test_all_in_commission_does_not_double_charge_included_components():
    with pytest.raises(TiveloError) as error:
        rules(fees=(fee(included_fee_ids=("handling",)), fee("handling"))).fees(
            instrument(), "buy", Decimal(100), Decimal(10), at=at(2)
        )
    assert error.value.code == ErrorCode.DATA_CONFLICT


def reference(value="10.05", **changes):
    values = dict(
        instrument_id="security-1",
        trading_date=date(2025, 1, 2),
        value=value,
        kind="previous_close",
        available_at=at(1, 7),
        source_id="synthetic",
        revision_id="p1",
    )
    values.update(changes)
    return ReferencePrice(**values)


def test_price_limits_use_explicit_reference_and_tick_rounding():
    bounds = rules().price_bounds(instrument(), reference(), at=at(2))
    assert (bounds.lower, bounds.upper) == (Decimal("9.05"), Decimal("11.06"))
    assert bounds.reference_revision == "p1"
    tiny = rules().price_bounds(instrument(), reference("0.02"), at=at(2))
    assert (tiny.lower, tiny.upper) == (Decimal("0.01"), Decimal("0.03"))


def test_unknown_bounds_are_different_from_unlimited():
    no_limits = dict(limit_up_ratio=None, limit_down_ratio=None)
    assert (
        rules(term=terms(price_limit_mode="unlimited", **no_limits))
        .price_bounds(instrument(), None, at=at(2))
        .state
        == "unlimited"
    )
    with pytest.raises(TiveloError) as error:
        rules(term=terms(price_limit_mode="unknown", **no_limits)).price_bounds(
            instrument(), None, at=at(2)
        )
    assert error.value.code == ErrorCode.RULES_UNAVAILABLE


@pytest.mark.parametrize(
    "value",
    [
        None,
        reference(available_at=at(3)),
        reference(trading_date=date(2025, 1, 3)),
        reference(instrument_id="other"),
    ],
)
def test_missing_future_or_wrong_reference_rejected(value):
    with pytest.raises(TiveloError):
        rules().price_bounds(instrument(), value, at=at(2))


@pytest.mark.parametrize(
    "changes",
    [
        dict(spec_revision="missing"),
        dict(tradable=False),
        dict(quote_currency="USD"),
        dict(timezone="America/New_York"),
        dict(available_at=at(3)),
        dict(valid_to=at(2)),
        dict(asset_class="future"),
    ],
)
def test_eligibility_is_historical_and_not_guessed_from_symbol(changes):
    with pytest.raises(TiveloError):
        rules().buy_quantity(instrument(**changes), Decimal(100), at=at(2))


def test_configuration_and_calendar_are_in_profile_fingerprint():
    original = rules()
    assert original.reference == rules().reference
    assert original.reference.sha256 != rules(term=terms(buy_increment="1")).reference.sha256
    assert original.reference.sha256 != rules(fees=(fee(rate="0.0003"),)).reference.sha256
    assert original.configuration == RuleConfiguration.model_validate_json(
        original.configuration.model_dump_json()
    )


def test_duplicate_terms_and_overlapping_fee_periods_are_rejected():
    with pytest.raises(ValidationError):
        RuleConfiguration(
            profile_id="fixture",
            profile_version="1",
            currency="CNY",
            terms=(terms(), terms()),
            required_fee_ids=("commission",),
            fee_rules=(fee(),),
        )
    with pytest.raises(ValidationError):
        rules(fees=(fee(), fee(revision_id="second")))


def test_execution_protocol_and_session_boundaries():
    book = rules()
    assert isinstance(book, MarketRules)
    assert isinstance(book, ExecutionRules)
    assert book.is_open(instrument(), at=at(2))
    assert not book.is_open(instrument(), at=at(2, 0))
    assert not book.is_open(instrument(), at=at(2, 7))
    assert not book.is_open(instrument(), at=at(4))


def test_fractional_cn_lots_and_nonrepresentable_fee_minima_rejected():
    with pytest.raises(TiveloError, match="whole shares"):
        rules(term=terms(buy_increment="0.1"))
    with pytest.raises(ValidationError, match="charge quantum"):
        fee(minimum="5.005")
