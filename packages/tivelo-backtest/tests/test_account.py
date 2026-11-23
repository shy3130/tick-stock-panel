"""Exact accounting scenarios against an explicit synthetic market policy."""

from datetime import UTC, datetime
from decimal import Decimal, Inexact, localcontext

import pytest
from tivelo_contracts.data import AdjustmentFactor, Bar, CorporateAction, Instrument
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import DefinitionReference, PriceViewSpec, Signal
from tivelo_contracts.market import FeeComponent, FeeQuote, PriceBounds, TradingTerms

from tivelo_backtest.account import CashEquityAccount
from tivelo_backtest.price_views import StaticPriceView


def at(day, hour=1):
    return datetime(2025, 1, day, hour, tzinfo=UTC)


def instrument(symbol="A", **changes):
    values = dict(
        instrument_id=symbol,
        venue_id="fixture",
        market_id="cn.equity",
        asset_class="equity",
        instrument_type="common_stock",
        display_symbol=symbol,
        name=symbol,
        quote_currency="CNY",
        settlement_currency="CNY",
        calendar_id="fixture",
        timezone="Asia/Shanghai",
        listing_date=dict(state="known", value="2020-01-01"),
        delisting_date=dict(state="not_yet"),
        tradable=True,
        spec_revision="fixture",
        valid_from=at(1, 0),
        available_at=at(1, 0),
        revision_id="1",
        source_id="fixture",
    )
    values.update(changes)
    return Instrument(**values)


class Rules:
    reference = DefinitionReference(definition_id="fixture", version="1", sha256="a" * 64)

    def trading_terms(self, security, *, at):
        return TradingTerms(
            spec_revision="fixture",
            market_id="cn.equity",
            venue_id="fixture",
            asset_class="equity",
            instrument_type="common_stock",
            quantity_unit="share",
            currency="CNY",
            price_tick="0.01",
            buy_minimum="1",
            buy_increment="1",
            buy_maximum="10000",
            sell_minimum="1",
            sell_increment="1",
            sell_maximum="10000",
            odd_lot_policy="all_remaining",
            sell_lag_sessions=1,
            price_limit_mode="unlimited",
            source_reference="synthetic",
            valid_from=security.valid_from,
            available_at=security.available_at,
            revision_id="1",
            source_id="fixture",
        )

    def is_open(self, security, *, at):
        return at.day in (2, 3, 6, 7) and 1 <= at.hour < 7

    def buy_quantity(self, security, quantity, *, at):
        return min(quantity // 1, Decimal(10000))

    def sell_quantity(self, security, quantity, *, available, at):
        return min(quantity // 1, available, Decimal(10000))

    def sellable_at(self, security, filled_at):
        return at({2: 3, 3: 6, 6: 7}[filled_at.day])

    def price_bounds(self, security, reference, *, at):
        return PriceBounds(state="unlimited", rule_revision="fixture")

    def fee_breakdown(self, security, side, quantity, raw_price, *, at):
        return FeeQuote(
            currency="CNY",
            notional=quantity * raw_price,
            components=(
                FeeComponent(
                    fee_id="commission",
                    amount="5",
                    rate="0",
                    minimum_applied=True,
                    rule_revision="fixture",
                    source_reference="synthetic",
                ),
            ),
            total="5",
        )


def signal(side="buy", symbol="A", decision=None, score="1"):
    return Signal(
        instrument_id=symbol,
        decision_at=decision or at(2, 7),
        side=side,
        score=score,
        source_id="fixture",
    )


def match(account, day, price="10", *, rules=None, hour=1, symbols=("A",)):
    account.match_open(
        at=at(day, hour),
        instruments={s: instrument(s) for s in symbols},
        raw_prices={s: Decimal(price) for s in symbols},
        references={},
        rules=rules or Rules(),
    )


def bought(**settings):
    account = CashEquityAccount("CNY", "2000", **settings)
    account.submit(signal(), Decimal(100), eligible_at=at(3))
    match(account, 3)
    return account


def test_signal_fill_dates_cash_fees_and_t_plus_one():
    account = bought()
    (position,) = account.snapshot(at(3, 7)).positions
    assert (position.quantity, position.sellable_quantity, position.cost) == (100, 0, 1005)
    assert account.snapshot(at(3, 7)).cash[0].available == 995
    assert account.snapshot(at(6)).positions[0].sellable_quantity == 100
    account.submit(signal("sell", decision=at(3, 7)), Decimal(100), eligible_at=at(6))
    match(account, 6, "11")
    assert account.snapshot(at(6)).cash[0].available == 2090
    assert account.snapshot(at(6)).positions == ()
    assert [event.fee for event in account.orders if event.status == "filled"] == [5, 5]
    assert sum(entry.cash_delta for entry in account.ledger) == 90
    assert account.orders[-1].decision_at == at(3, 7)
    assert account.orders[-1].filled_at == at(6)


def test_buy_and_sell_same_session_cannot_bypass_sellable_quantity():
    account = bought()
    account.submit(signal("sell", decision=at(3, 2)), Decimal(100), eligible_at=at(3, 3))
    match(account, 3, hour=3)
    assert account.orders[-1].status == "expired"
    assert account.orders[-1].reason == "insufficient_sellable_quantity"
    assert account.snapshot(at(3, 3)).positions[0].quantity == 100


def test_same_close_fill_is_rejected():
    with pytest.raises(TiveloError) as error:
        CashEquityAccount("CNY", "1000").submit(signal(), Decimal(10), eligible_at=at(2, 7))
    assert error.value.code == ErrorCode.FUTURE_INFORMATION


def test_cash_shortage_uses_stable_candidate_order_and_does_not_overdraw():
    account = CashEquityAccount("CNY", "1005")
    for symbol in ("B", "A"):
        account.submit(signal(symbol=symbol), Decimal(100), eligible_at=at(3))
    match(account, 3, symbols=("B", "A"))
    assert [event.instrument_id for event in account.orders if event.status == "filled"] == ["A"]
    assert account.snapshot(at(3)).cash[0].available == 0
    assert any(event.reason == "insufficient_cash" for event in account.orders)


def test_gapped_open_does_not_resize_or_leverage_order():
    account = CashEquityAccount("CNY", "1005")
    account.submit(signal(), Decimal(100), eligible_at=at(3))
    match(account, 3, "11")
    assert account.orders[-1].reason == "insufficient_cash"
    assert account.snapshot(at(3)).cash[0].available == 1005
    assert account.ledger == ()


def test_gtc_limit_block_is_deferred_and_later_uses_original_decision():
    class Limited(Rules):
        def price_bounds(self, security, reference, *, at):
            return PriceBounds(
                state="limited", lower="9", upper="11", reference_revision="p1", rule_revision="r1"
            )

    account = CashEquityAccount("CNY", "2000", order_expiry="good_till_cancelled")
    account.submit(signal(), Decimal(100), eligible_at=at(3))
    match(account, 3, "11", rules=Limited())
    assert account.orders[-1].status == "accepted"
    assert account.orders[-1].reason == "limit_up"
    match(account, 6, "10", rules=Limited())
    assert account.orders[-1].status == "filled"
    assert account.orders[-1].decision_at == at(2, 7)


def test_end_keeps_holdings_and_only_expires_unfilled_orders():
    account = bought()
    account.submit(signal("sell", decision=at(3, 7)), Decimal(100), eligible_at=at(6))
    account.finish(at(3, 7))
    assert account.orders[-1].status == "expired"
    assert account.snapshot(at(3, 7)).positions[0].quantity == 100
    assert account.mark_to_market(at(3, 7), {"A": Decimal(11)}).equity == 2095


def action(kind="split", **changes):
    values = dict(
        action_id="event-1",
        instrument_id="A",
        action_type=kind,
        effective_at=at(6, 0),
        available_at=at(2, 0),
        revision_id="1",
        source_id="fixture",
    )
    values.update(
        dict(quantity_ratio="2")
        if kind == "split"
        else dict(cash_per_unit="1", currency="CNY", record_at=at(3, 7), payment_at=at(7, 0))
    )
    values.update(changes)
    return CorporateAction(**values)


def test_split_preserves_cost_and_equity_without_double_return():
    account = bought()
    account.apply_action(action(), at=at(6, 0))
    account.apply_action(action(), at=at(6, 0))
    (position,) = account.snapshot(at(6)).positions
    assert position.quantity == 200 and position.cost == 1005
    assert account.mark_to_market(at(6), {"A": Decimal(5)}).equity == 1995
    assert len([row for row in account.ledger if row.kind == "split"]) == 1


def test_dividend_entitlement_receivable_payment_and_idempotence():
    account = bought(dividend_treatment="gross_before_personal_tax")
    dividend = action("cash_dividend")
    account.capture_entitlement(dividend, at=at(3, 7))
    account.apply_action(dividend, at=at(6, 0))
    snapshot = account.snapshot(at(6))
    assert snapshot.cash[0].available == 995 and snapshot.cash[0].receivable == 100
    assert account.mark_to_market(at(6), {"A": Decimal(9)}).equity == 1995
    account.pay_dividend(dividend, at=at(7, 0))
    account.pay_dividend(dividend, at=at(7, 0))
    assert account.snapshot(at(7)).cash[0].available == 1095
    assert account.snapshot(at(7)).cash[0].receivable == 0
    assert account.mark_to_market(at(7), {"A": Decimal(9)}).equity == 1995
    assert "gross_dividends_before_personal_tax" in account.diagnostics


def test_unknown_dividend_policy_or_entitlement_cannot_fabricate_cash():
    account = bought()
    with pytest.raises(TiveloError):
        account.apply_action(action("cash_dividend"), at=at(6, 0))
    explicit = bought(dividend_treatment="gross_before_personal_tax")
    with pytest.raises(TiveloError):
        explicit.apply_action(action("cash_dividend"), at=at(6, 0))
    assert explicit.snapshot(at(6)).cash[0].available == 995


def test_corporate_action_cancels_old_price_orders():
    account = bought(order_expiry="good_till_cancelled")
    account.submit(signal("sell", decision=at(3, 7)), Decimal(100), eligible_at=at(6))
    account.apply_action(action(), at=at(6, 0))
    assert account.orders[-1].status == "cancelled"
    assert account.orders[-1].reason == "corporate_action_changed_basis"


def test_future_action_and_unsupported_asset_currency_fail():
    account = bought()
    with pytest.raises(TiveloError) as error:
        account.apply_action(action(available_at=at(7)), at=at(6, 0))
    assert error.value.code == ErrorCode.FUTURE_INFORMATION
    for change in (dict(asset_class="future"), dict(quote_currency="USD")):
        other = CashEquityAccount("CNY", "2000")
        other.submit(signal(), Decimal(100), eligible_at=at(3))
        with pytest.raises(TiveloError):
            other.match_open(
                at=at(3),
                instruments={"A": instrument(**change)},
                raw_prices={"A": Decimal(10)},
                references={},
                rules=Rules(),
            )


def test_missing_mark_and_backwards_clock_are_not_silently_accepted():
    account = bought()
    with pytest.raises(TiveloError):
        account.mark_to_market(at(3), {})
    with pytest.raises(TiveloError):
        account.snapshot(at(2))


def test_candidate_priority_and_dividend_payment_ignore_ambient_precision():
    account = CashEquityAccount("CNY", "1005")
    for symbol, score in (("A", "1.001"), ("B", "1.002")):
        account.submit(signal(symbol=symbol, score=score), Decimal(100), eligible_at=at(3))
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        match(account, 3, symbols=("A", "B"))
    assert [row.instrument_id for row in account.orders if row.status == "filled"] == ["B"]

    dividend_account = bought(dividend_treatment="gross_before_personal_tax")
    dividend = action("cash_dividend", cash_per_unit="0.123456789012")
    dividend_account.capture_entitlement(dividend, at=at(3, 7))
    dividend_account.apply_action(dividend, at=at(6, 0))
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        dividend_account.pay_dividend(dividend, at=at(7, 0))
        final = dividend_account.snapshot(at(7))
    assert final.cash[0].total == Decimal("1007.3456789012")
    assert sum(row.receivable_delta for row in dividend_account.ledger) == 0


def test_fifo_partial_sale_preserves_remaining_cost_and_locked_lot():
    account = CashEquityAccount("CNY", "5000")
    account.submit(signal(decision=at(2, 0)), Decimal(100), eligible_at=at(2))
    match(account, 2)
    account.submit(signal(), Decimal(100), eligible_at=at(3))
    match(account, 3, "12")
    account.submit(signal("sell", decision=at(3, 2)), Decimal(37), eligible_at=at(3, 3))
    match(account, 3, "11", hour=3)
    (position,) = account.snapshot(at(3, 3)).positions
    assert (position.quantity, position.sellable_quantity, position.cost) == (
        163,
        63,
        Decimal("1838.15"),
    )
    account.submit(signal("sell", decision=at(3, 4)), Decimal(163), eligible_at=at(6))
    match(account, 6, "11")
    assert account.snapshot(at(6)).positions == ()
    assert account.snapshot(at(6)).cash[0].total == 4980
    assert sum(row.quantity_delta for row in account.ledger) == 0
    assert sum(row.cash_delta for row in account.ledger) == -20


def test_rejected_fractional_split_does_not_modify_positions_or_cash():
    account = bought()
    before = account.snapshot(at(3))
    with pytest.raises(TiveloError) as error:
        account.apply_action(action(quantity_ratio="0.333"), at=at(6, 0))
    assert error.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
    after = account.snapshot(at(6, 0))
    assert (before.cash, before.positions[0].quantity, before.positions[0].cost) == (
        after.cash,
        after.positions[0].quantity,
        after.positions[0].cost,
    )
    assert [row.kind for row in account.ledger] == ["buy"]


def test_record_date_entitlement_survives_sale_before_payment():
    account = bought(dividend_treatment="gross_before_personal_tax")
    dividend = action("cash_dividend")
    account.capture_entitlement(dividend, at=at(3, 7))
    account.submit(signal("sell", decision=at(3, 7)), Decimal(100), eligible_at=at(6))
    account.apply_action(dividend, at=at(6, 0))
    # Ex-date cancels a stale price/quantity instruction. Submit a fresh one.
    account.submit(signal("sell", decision=at(6, 0)), Decimal(100), eligible_at=at(6))
    match(account, 6, "9")
    assert account.snapshot(at(6)).positions == ()
    account.pay_dividend(dividend, at=at(7, 0))
    assert account.snapshot(at(7)).cash[0].total == 1990


def test_revised_action_cannot_reapply_or_pay_another_amount():
    account = bought(dividend_treatment="gross_before_personal_tax")
    dividend = action("cash_dividend")
    account.capture_entitlement(dividend, at=at(3, 7))
    account.apply_action(dividend, at=at(6, 0))
    revision = action("cash_dividend", revision_id="2", cash_per_unit="2")
    with pytest.raises(TiveloError) as error:
        account.apply_action(revision, at=at(6, 0))
    assert error.value.code == ErrorCode.DATA_CONFLICT
    with pytest.raises(TiveloError):
        account.pay_dividend(revision, at=at(7, 0))
    assert account.snapshot(at(7)).cash[0].receivable == 100


def test_account_policy_is_immutable_and_includes_execution_semantics():
    account = CashEquityAccount("CNY", "2000")
    assert account.reference == CashEquityAccount("CNY", "3000").reference
    assert (
        account.reference
        != CashEquityAccount("CNY", "2000", order_expiry="good_till_cancelled").reference
    )
    assert (
        account.reference
        != CashEquityAccount(
            "CNY", "2000", dividend_treatment="gross_before_personal_tax"
        ).reference
    )
    with pytest.raises(AttributeError):
        account.currency = "USD"
    assert account.policy.cost_allocation == "fifo_decimal12_half_even"


def test_sale_with_fees_above_proceeds_cannot_overdraw_cash():
    account = CashEquityAccount("CNY", "1005")
    account.submit(signal(), Decimal(100), eligible_at=at(3))
    match(account, 3)
    account.submit(signal("sell", decision=at(3, 7)), Decimal(100), eligible_at=at(6))
    match(account, 6, "0.01")
    assert account.orders[-1].reason == "insufficient_cash"
    assert account.snapshot(at(6)).positions[0].quantity == 100
    assert account.snapshot(at(6)).cash[0].available == 0


def test_submission_idempotence_and_cancellation_preserve_account():
    account = CashEquityAccount("CNY", "2000")
    first = account.submit(signal(), Decimal(100), eligible_at=at(3))
    assert account.submit(signal(), Decimal(100), eligible_at=at(3)) == first
    assert len(account.orders) == 2
    with pytest.raises(TiveloError) as error:
        account.submit(signal(), Decimal(200), eligible_at=at(3))
    assert error.value.code == ErrorCode.DATA_CONFLICT
    account.finish(at(2, 7), cancelled=True)
    assert account.orders[-1].status == "cancelled"
    assert account.orders[-1].event_at == at(2, 7)
    with pytest.raises(TiveloError):
        match(account, 3)
    assert account.ledger == ()


@pytest.mark.parametrize("mode,limit", [("raw", "5"), ("qfq", "10"), ("hfq", "10")])
def test_adjusted_limit_is_converted_once_but_fill_and_valuation_stay_raw(mode, limit):
    bar = Bar(
        instrument_id="A",
        timeframe="1d",
        bar_start=at(2),
        bar_end=at(2, 7),
        trading_date=at(2).date(),
        session_id="fixture-2",
        open="5",
        high="5",
        low="5",
        close="5",
        volume="100",
        turnover="500",
        quantity_unit="share",
        quote_currency="CNY",
        price_basis="raw",
        available_at=at(2, 7),
        ingested_at=at(2, 7),
        source_id="fixture",
        revision_id="1",
    )
    factors = tuple(
        AdjustmentFactor(
            factor_id=f"f{day}",
            instrument_id="A",
            effective_at=at(day, 0),
            available_at=at(day, 0),
            value=value,
            definition_id="cumulative-v1",
            factor_kind="cumulative_forward",
            baseline_id="listing",
            source_id="fixture",
            revision_id="1",
        )
        for day, value in ((1, "2"), (2, "4"))
    )
    specification = PriceViewSpec(
        provider_id=f"tivelo.{mode}",
        mode=mode,
        **(
            {}
            if mode == "raw"
            else dict(
                anchor_at=at(1, 0),
                factor_definition_id="cumulative-v1",
                factor_baseline_id="listing",
            )
        ),
    )
    view = StaticPriceView(mode)
    projected = view.project(
        (bar,), factors if mode != "raw" else (), specification, decision_at=at(2, 7)
    )
    assert projected[0].close == Decimal(limit)
    price = view.price(limit, instrument_id="A", decision_at=at(2, 7))
    instruction = Signal(**{**signal().model_dump(), "limit_price": price})
    account = CashEquityAccount("CNY", "2000")
    with pytest.raises(TiveloError) as error:
        account.submit(instruction, Decimal(100), eligible_at=at(3))
    assert error.value.code == ErrorCode.PRICE_BASIS_UNPROVEN
    account.submit(instruction, Decimal(100), eligible_at=at(3), price_view=view)
    match(account, 3, "5")
    assert account.orders[-1].raw_fill_price == 5
    assert account.orders[-1].order_type == "limit"
    assert account.orders[-1].raw_limit_price == 5
    assert account.snapshot(at(3)).cash[0].available == 1495
    assert account.mark_to_market(at(3), {"A": Decimal(5)}).equity == 1995

    gap = CashEquityAccount("CNY", "2000")
    gap.submit(instruction, Decimal(100), eligible_at=at(3), price_view=view)
    match(gap, 3, "5.01")
    assert gap.orders[-1].reason == "limit_price_not_met"
    assert gap.ledger == ()


def test_same_provider_action_id_on_different_instruments_has_separate_entitlements():
    account = CashEquityAccount("CNY", "4000", dividend_treatment="gross_before_personal_tax")
    for symbol in ("A", "B"):
        account.submit(signal(symbol=symbol), Decimal(100), eligible_at=at(3))
    match(account, 3, symbols=("A", "B"))
    dividends = (
        action("cash_dividend", action_id="same-id", instrument_id="A", cash_per_unit="1"),
        action("cash_dividend", action_id="same-id", instrument_id="B", cash_per_unit="2"),
    )
    for dividend in dividends:
        account.capture_entitlement(dividend, at=at(3, 7))
    for dividend in dividends:
        account.apply_action(dividend, at=at(6, 0))
    assert account.snapshot(at(6)).cash[0].receivable == 300
    for dividend in dividends:
        account.pay_dividend(dividend, at=at(7, 0))
    assert account.snapshot(at(7)).cash[0].total == 2290
    assert account.snapshot(at(7)).cash[0].receivable == 0
