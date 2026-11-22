"""Execution and sizing use the same explicit order-type contract, without CN installed."""

from decimal import ROUND_DOWN, Decimal

import pytest
from test_account import Rules as AccountRules
from test_account import at, instrument, match, signal
from test_daily import Rules as DailyRules
from test_daily import ScheduledStrategy, bar, case, run
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import PriceViewSpec, RunLock, Signal, SignalBatch
from tivelo_contracts.market import OrderQuantityTerms, PriceBounds

from tivelo_backtest.account import CashEquityAccount
from tivelo_backtest.order_rules import bind_order_rules
from tivelo_backtest.price_views import StaticPriceView


class TypedRules:
    supported_types = ("limit", "market")

    def ceiling(self, order_type, at):
        return Decimal(50000 if order_type == "market" else 100000)

    def trading_terms(self, security, *, at):
        return (
            super()
            .trading_terms(security, at=at)
            .model_copy(
                update={
                    "order_quantity_rules": tuple(
                        OrderQuantityTerms(
                            order_type=kind,
                            buy_minimum="200",
                            buy_increment="1",
                            buy_maximum=self.ceiling(kind, at),
                            sell_minimum="200",
                            sell_increment="1",
                            sell_maximum=self.ceiling(kind, at),
                            odd_lot_policy="all_remaining",
                        )
                        for kind in self.supported_types
                    )
                }
            )
        )

    def quantity_terms(self, security, order_type, *, at):
        return next(
            q
            for q in self.trading_terms(security, at=at).order_quantity_rules
            if q.order_type == order_type
        )

    def order_quantity(self, security, side, quantity, *, order_type, available=None, at):
        maximum = self.ceiling(order_type, at)
        if side == "sell" and quantity >= available and available <= maximum:
            return available
        result = min(quantity, maximum, available if side == "sell" else maximum)
        return result.to_integral_value(rounding=ROUND_DOWN) if result >= 200 else Decimal(0)


class TypedAccountRules(TypedRules, AccountRules):
    pass


class TypedDailyRules(TypedRules, DailyRules):
    commission = Decimal(5)


@pytest.mark.parametrize(
    "offered,available,accepted",
    [
        ("99", "299", True),
        ("199", "299", True),
        ("100", "299", True),
        ("0", "99", True),
        ("98", "299", False),
        ("198", "299", False),
        ("99", "300", False),
        ("199.5", "299", False),
        ("299", "299", False),
        ("199", "99", False),
    ],
)
def test_consumer_independently_validates_partial_remainder_sales(offered, available, accepted):
    class Proposed(AccountRules):
        def trading_terms(self, security, *, at):
            return (
                super()
                .trading_terms(security, at=at)
                .model_copy(
                    update={
                        "sell_minimum": Decimal(100),
                        "sell_increment": Decimal(100),
                        "sell_maximum": Decimal(200),
                        "odd_lot_policy": "remainder_once",
                    }
                )
            )

        def sell_quantity(self, security, quantity, *, available, at):
            return Decimal(offered)

    binding = bind_order_rules(Proposed(), instrument(), "market", at=at(3))
    if accepted:
        assert binding.quantity("sell", Decimal(299), available=Decimal(available)) == Decimal(
            offered
        )
    else:
        with pytest.raises(TiveloError) as error:
            binding.quantity("sell", Decimal(299), available=Decimal(available))
        assert error.value.code == ErrorCode.CONTRACT_INVALID


def test_consumer_rejects_unchecked_remainder_terms_with_an_offset_grid():
    class Invalid(AccountRules):
        def trading_terms(self, security, *, at):
            return (
                super()
                .trading_terms(security, at=at)
                .model_copy(
                    update={
                        "sell_minimum": Decimal(200),
                        "sell_increment": Decimal(100),
                        "odd_lot_policy": "remainder_once",
                    }
                )
            )

    with pytest.raises((TiveloError, ValueError)):
        binding = bind_order_rules(Invalid(), instrument(), "market", at=at(3))
        binding.quantity("sell", Decimal(199), available=Decimal(299))


def test_daily_target_sizing_and_execution_both_accept_a_complete_remainder():
    class Proposed(DailyRules):
        def trading_terms(self, security, *, at):
            return (
                super()
                .trading_terms(security, at=at)
                .model_copy(
                    update={
                        "buy_minimum": Decimal(1),
                        "buy_increment": Decimal(1),
                        "buy_maximum": Decimal(299),
                        "odd_lot_policy": "remainder_once",
                    }
                )
            )

        def buy_quantity(self, security, quantity, *, at):
            assert quantity >= 299
            return Decimal(299)

        def sell_quantity(self, security, quantity, *, available, at):
            # The independent provider offers one known-valid partial sale;
            # the core must calculate its requested target and verify the grid.
            assert quantity == 199 and available == 299
            return Decimal(199)

    class Trim(ScheduledStrategy):
        def decide(self, context):
            batch = super().decide(context)
            return batch.model_copy(
                update={
                    "signals": tuple(
                        item.model_copy(update={"target_weight": Decimal("0.1")})
                        if item.side == "sell"
                        else item
                        for item in batch.signals
                    )
                }
            )

    result = run(
        case(rules=Proposed(), strategy=Trim(), bars=tuple(bar(day, "10") for day in (2, 3, 6)))
    )
    assert [event.filled_quantity for event in result.orders if event.status == "filled"] == [
        299,
        199,
    ]
    assert result.final_portfolio.cash[0].total == 9000
    assert result.final_portfolio.positions[0].quantity == 100
    assert result.equity[-1].equity == 10000


def limit_signal(side="buy", *, decision=None, limit="10"):
    decision = decision or at(2, 7)
    view = StaticPriceView("raw")
    view.project(
        (bar(decision.day, "10"),),
        (),
        PriceViewSpec(provider_id=view.provider_id, mode="raw"),
        decision_at=decision,
    )
    instruction = Signal(
        **{
            **signal(side, decision=decision).model_dump(),
            "limit_price": view.price(limit, instrument_id="A", decision_at=decision),
        }
    )
    return instruction, view


@pytest.mark.parametrize("order_type", ["market", "limit"])
def test_same_quantity_is_accepted_only_for_a_type_with_sufficient_ceiling(order_type):
    account = CashEquityAccount("CNY", "2000000")
    instruction, view = limit_signal() if order_type == "limit" else (signal(), None)
    account.submit(instruction, Decimal(60000), eligible_at=at(3), price_view=view)
    match(account, 3, rules=TypedAccountRules())
    assert all(event.order_type == order_type for event in account.orders)
    if order_type == "limit":
        assert account.orders[-1].status == "filled"
        assert account.orders[-1].raw_limit_price == 10
        assert account.snapshot(at(3)).cash[0].available == 1399995
        assert account.snapshot(at(3)).positions[0].quantity == 60000
    else:
        assert account.orders[-1].reason == "invalid_quantity"
        assert account.orders[-1].raw_limit_price is None
        assert account.ledger == ()
        assert account.snapshot(at(3)).cash[0].available == 2000000


@pytest.mark.parametrize("order_type", ["market", "limit"])
def test_sell_all_does_not_bypass_type_specific_ceiling(order_type):
    account = CashEquityAccount("CNY", "2000000")
    instruction, view = limit_signal()
    account.submit(instruction, Decimal(60000), eligible_at=at(3), price_view=view)
    match(account, 3, rules=TypedAccountRules())
    instruction, view = (
        limit_signal("sell", decision=at(3, 7))
        if order_type == "limit"
        else (signal("sell", decision=at(3, 7)), None)
    )
    account.submit(instruction, Decimal(60000), eligible_at=at(6), price_view=view)
    match(account, 6, rules=TypedAccountRules())
    assert account.snapshot(at(6)).cash[0].available == (
        1999990 if order_type == "limit" else 1399995
    )
    assert len(account.snapshot(at(6)).positions) == (0 if order_type == "limit" else 1)


def test_execution_rechecks_changed_type_limits_without_resizing_existing_order():
    class Changed(TypedAccountRules):
        def ceiling(self, order_type, at):
            return Decimal(100000 if at.day == 2 else 50000)

    market = Changed()
    assert (
        market.order_quantity(instrument(), "buy", Decimal(60000), order_type="market", at=at(2))
        == 60000
    )
    account = CashEquityAccount("CNY", "2000000")
    account.submit(signal(), Decimal(60000), eligible_at=at(3))
    match(account, 3, rules=market)
    assert account.orders[-1].requested_quantity == 60000
    assert account.orders[-1].filled_quantity == 0
    assert account.orders[-1].reason == "invalid_quantity"
    assert account.snapshot(at(3)).cash[0].available == 2000000


def test_disallowed_order_type_expires_with_an_explicit_reason():
    class LimitOnly(TypedAccountRules):
        supported_types = ("limit",)

    account = CashEquityAccount("CNY", "2000000")
    account.submit(signal(), Decimal(200), eligible_at=at(3))
    match(account, 3, rules=LimitOnly())
    assert account.orders[-1].status == "expired"
    assert account.orders[-1].reason == "unsupported_order_type"
    assert account.ledger == ()


@pytest.mark.parametrize("typed", [False, True])
def test_limit_price_must_align_with_the_execution_tick(typed):
    account = CashEquityAccount("CNY", "2000000")
    instruction, view = limit_signal(limit="10.005")
    account.submit(instruction, Decimal(200), eligible_at=at(3), price_view=view)
    match(account, 3, rules=TypedAccountRules() if typed else AccountRules())
    assert account.orders[-1].reason == "invalid_limit_tick"
    assert account.ledger == ()
    assert account.snapshot(at(3)).cash[0].available == 2000000


@pytest.mark.parametrize("side,limit", [("buy", "12"), ("sell", "8")])
def test_favorable_fill_does_not_make_an_out_of_band_limit_order_legal(side, limit):
    class Bounded(TypedAccountRules):
        def price_bounds(self, security, reference, *, at):
            return PriceBounds(
                state="limited", lower="9", upper="11", reference_revision="p1", rule_revision="r1"
            )

    account = CashEquityAccount("CNY", "2000000")
    if side == "sell":
        account.submit(signal(), Decimal(200), eligible_at=at(3))
        match(account, 3, rules=Bounded())
    decision, execution = (at(2, 7), 3) if side == "buy" else (at(3, 7), 6)
    instruction, view = limit_signal(side, decision=decision, limit=limit)
    account.submit(instruction, Decimal(200), eligible_at=at(execution), price_view=view)
    before = account.ledger
    match(account, execution, rules=Bounded())
    assert account.orders[-1].reason == "limit_price_out_of_bounds"
    assert account.ledger == before


def test_old_provider_cannot_claim_typed_terms_but_ignore_them():
    class Missing(AccountRules):
        def trading_terms(self, security, *, at):
            return TypedAccountRules().trading_terms(security, at=at)

    account = CashEquityAccount("CNY", "2000000")
    account.submit(signal(), Decimal(200), eligible_at=at(3))
    with pytest.raises(TiveloError) as error:
        match(account, 3, rules=Missing())
    assert error.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
    assert account.ledger == ()


@pytest.mark.parametrize("fault", ["terms", "quantity"])
def test_provider_cannot_contradict_declared_limits_or_overallocate(fault):
    class Faulty(TypedAccountRules):
        def quantity_terms(self, security, order_type, *, at):
            value = super().quantity_terms(security, order_type, at=at)
            return (
                value.model_copy(update={"buy_maximum": Decimal(1000000)})
                if fault == "terms"
                else value
            )

        def order_quantity(self, security, side, quantity, **options):
            return quantity + 1

    account = CashEquityAccount("CNY", "2000000")
    account.submit(signal(), Decimal(200), eligible_at=at(3))
    with pytest.raises(TiveloError) as error:
        match(account, 3, rules=Faulty())
    assert error.value.code == ErrorCode.CONTRACT_INVALID
    assert account.ledger == ()


class LimitStrategy(ScheduledStrategy):
    def decide(self, context):
        original = super().decide(context)
        return SignalBatch(
            decision_at=context.now,
            signals=tuple(
                Signal(
                    **{
                        **instruction.model_dump(),
                        "limit_price": self.view.price(
                            "12" if instruction.side == "buy" else "10",
                            instrument_id=instruction.instrument_id,
                            decision_at=context.now,
                        ),
                    }
                )
                for instruction in original.signals
            ),
        )


@pytest.mark.parametrize(
    "order_type,capital,quantity,final_cash",
    [
        ("market", "1000005", 50000, 1049995),
        ("limit", "1000005", 100000, 1099995),
        ("limit", "1000000", 99999, 1099989),
    ],
)
def test_daily_sizing_reserves_fees_and_uses_the_type_specific_lot_grid(
    order_type, capital, quantity, final_cash
):
    strategy = LimitStrategy() if order_type == "limit" else ScheduledStrategy()
    runner, lock, reader, binding = case(rules=TypedDailyRules(), strategy=strategy)
    strategy.view = runner.price_view
    lock = RunLock.create(
        lock.contents.model_copy(
            update={
                "spec": lock.contents.spec.model_copy(update={"initial_capital": Decimal(capital)})
            }
        )
    )
    result = runner.run(lock, reader, binding)
    fills = [event for event in result.orders if event.status == "filled"]
    assert [(event.side, event.filled_quantity) for event in fills] == [
        ("buy", quantity),
        ("sell", quantity),
    ]
    assert all(event.order_type == order_type for event in result.orders)
    assert [event.fee for event in fills] == [5, 5]
    assert result.final_portfolio.cash[0].available == final_cash
    assert result.final_portfolio.positions == ()


def test_daily_does_not_create_a_market_order_for_a_limit_only_profile():
    class LimitOnly(TypedDailyRules):
        supported_types = ("limit",)

    result = run(case(rules=LimitOnly()))
    assert result.orders == ()
    assert {event.code for event in result.diagnostic_events} == {"unsupported_order_type"}
    assert result.final_portfolio.cash[0].available == 10000


@pytest.mark.parametrize("typed,quantity", [(False, 900), (True, 996)])
def test_fractional_sizing_budget_is_floored_at_the_contract_boundary(typed, quantity):
    market = TypedDailyRules() if typed else DailyRules()
    market.commission = Decimal(5)
    result = run(
        case(
            rules=market,
            bars=tuple(bar(day, "10.03") for day in (2, 3, 6)),
        )
    )
    assert [event.filled_quantity for event in result.orders if event.status == "filled"] == [
        quantity,
        quantity,
    ]
    assert result.final_portfolio.cash[0].available == 9990


@pytest.mark.parametrize(
    "typed,bought,sold,remaining", [(False, 900, 500, 400), (True, 996, 664, 332)]
)
def test_fractional_sell_target_preserves_the_correct_remaining_position(
    typed, bought, sold, remaining
):
    class Trim(ScheduledStrategy):
        def decide(self, context):
            batch = super().decide(context)
            return batch.model_copy(
                update={
                    "signals": tuple(
                        s.model_copy(update={"target_weight": Decimal("0.333")})
                        if s.side == "sell"
                        else s
                        for s in batch.signals
                    )
                }
            )

    market = TypedDailyRules() if typed else DailyRules()
    market.commission = Decimal(5)
    result = run(
        case(
            rules=market,
            strategy=Trim(),
            bars=tuple(bar(day, "10.03") for day in (2, 3, 6)),
        )
    )
    assert [event.filled_quantity for event in result.orders if event.status == "filled"] == [
        bought,
        sold,
    ]
    assert result.final_portfolio.positions[0].quantity == remaining
    assert result.equity[-1].equity == 9990
