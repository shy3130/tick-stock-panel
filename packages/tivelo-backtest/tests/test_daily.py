from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from tivelo_contracts.data import (
    AdjustmentFactor,
    Bar,
    CorporateAction,
    DatasetScope,
    Instrument,
    OpeningObservation,
    ProjectedBar,
)
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import (
    BacktestSpec,
    DefinitionReference,
    ExecutionModel,
    ExtensionPin,
    PackageReference,
    PriceViewSpec,
    RunLock,
    RunLockContents,
    RuntimeReference,
    Signal,
    SignalBatch,
)
from tivelo_contracts.market import FeeComponent, FeeQuote, PriceBounds, TradingDay, TradingTerms
from tivelo_contracts.testing import InMemoryReader

from tivelo_backtest.account import CashEquityAccount
from tivelo_backtest.daily import VERIFIER_VERSION, DailyBacktest, StrategyBinding
from tivelo_backtest.price_views import StaticPriceView


def at(day, hour=1):
    return datetime(2025, 1, day, hour, tzinfo=UTC)


class Rules:
    market_id, version = "cn.equity", "1"
    buy_cost_monotone = True
    reference = DefinitionReference(definition_id="market.fixture", version="1", sha256="c" * 64)
    dates = (2, 3, 6, 7, 8)
    commission = Decimal(0)

    def trading_days(self, start, end, *, as_of):
        return tuple(
            TradingDay(
                calendar_id="fixture",
                trading_date=date(2025, 1, day),
                open_at=at(day),
                close_at=at(day, 7),
                bar_convention_id="fixture.day",
                available_at=at(1, 0),
            )
            for day in self.dates
            if start <= date(2025, 1, day) <= end
        )

    def next_open(self, after_date, *, as_of):
        return next(at(day) for day in self.dates if day > after_date.day)

    def supports(self, security):
        return security.asset_class == "equity" and security.market_id == self.market_id

    def trading_terms(self, security, *, at):
        return TradingTerms(
            spec_revision="fixture",
            market_id=self.market_id,
            venue_id="fixture",
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
            price_limit_mode="unlimited",
            source_reference="synthetic",
            valid_from=security.valid_from,
            available_at=security.available_at,
            revision_id="1",
            source_id="synthetic",
        )

    def is_open(self, security, *, at):
        return at.day in self.dates and 1 <= at.hour < 7

    def buy_quantity(self, security, quantity, *, at):
        return min(quantity // 100 * 100, Decimal(1000000))

    def sell_quantity(self, security, quantity, *, available, at):
        return available if quantity >= available else quantity // 100 * 100

    def sellable_at(self, security, filled_at):
        return self.next_open(filled_at.date(), as_of=filled_at)

    def price_bounds(self, security, reference, *, at):
        return PriceBounds(state="unlimited", rule_revision="1")

    def fee_breakdown(self, security, side, quantity, raw_price, *, at):
        return FeeQuote(
            currency="CNY",
            notional=quantity * raw_price,
            total=self.commission,
            components=(
                FeeComponent(
                    fee_id="commission",
                    amount=self.commission,
                    rate="0",
                    minimum_applied=bool(self.commission),
                    rule_revision="1",
                    source_reference="synthetic",
                ),
            ),
        )

    def fees(self, *args, **kwargs):
        return self.fee_breakdown(*args, **kwargs).total


def bar(day, price, **changes):
    values = dict(
        instrument_id="A",
        timeframe="1d",
        bar_start=at(day),
        bar_end=at(day, 7),
        trading_date=date(2025, 1, day),
        session_id="fixture.day",
        open=price,
        high=price,
        low=price,
        close=price,
        volume="10000",
        turnover="100000",
        quantity_unit="share",
        quote_currency="CNY",
        price_basis="raw",
        available_at=at(day, 7),
        ingested_at=at(day, 7),
        source_id="synthetic",
        revision_id="1",
    )
    values.update(changes)
    return Bar(**values)


def opening(day, price, **changes):
    values = dict(
        instrument_id="A",
        event_at=at(day),
        trading_date=date(2025, 1, day),
        session_id="fixture.open",
        state="tradable",
        raw_open=price,
        quote_currency="CNY",
        available_at=at(day),
        ingested_at=at(day),
        source_id="synthetic",
        revision_id="1",
    )
    values.update(changes)
    return OpeningObservation(**values)


def security():
    return Instrument(
        instrument_id="A",
        market_id="cn.equity",
        venue_id="fixture",
        asset_class="equity",
        instrument_type="common_stock",
        display_symbol="A",
        name="Synthetic",
        quote_currency="CNY",
        settlement_currency="CNY",
        calendar_id="fixture",
        timezone="Asia/Shanghai",
        tradable=True,
        spec_revision="fixture",
        listing_date=dict(state="known", value="2020-01-01"),
        delisting_date=dict(state="not_yet"),
        valid_from=at(1, 0),
        available_at=at(1, 0),
        revision_id="1",
        source_id="synthetic",
    )


class ScheduledStrategy:
    def __init__(self, signals=None):
        self.signals = {2: "buy", 3: "sell"} if signals is None else signals
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        side = self.signals.get(context.now.day)
        return SignalBatch(
            decision_at=context.now,
            signals=()
            if side is None
            else (
                Signal(
                    instrument_id="A",
                    decision_at=context.now,
                    side=side,
                    score="1",
                    source_id="strategy.fixture",
                    target_weight="1" if side == "buy" else "0",
                ),
            ),
        )


def case(
    *,
    bars=None,
    openings=None,
    actions=(),
    factors=(),
    mode="raw",
    anchor=1,
    rules=None,
    knowledge="historical_versions",
    strategy=None,
    end=6,
    start=2,
    warmup=None,
    execution=None,
    dividend_treatment="reject",
    field_scopes=None,
    extension_values=None,
):
    bars = (
        tuple(bar(day, price) for day, price in ((2, "10"), (3, "10"), (6, "11")))
        if bars is None
        else bars
    )
    openings = (
        tuple(
            opening(day, str(b.open))
            for day in Rules.dates
            for b in bars
            if b.trading_date.day == day and b.revision_id == "1"
        )
        if openings is None
        else openings
    )
    scope = DatasetScope(
        market_id="cn.equity",
        asset_class="equity",
        instrument_ids=("A",),
        start=at(1, 0),
        end=at(9, 0),
        timeframes=("1d",),
        bar_convention_id="fixture.day",
    )
    scopes = {
        name: scope for name in ("bar.raw", "instrument", "corporate_action", "opening_observation")
    }
    if factors:
        scopes["adjustment_factor"] = scope
    reader = InMemoryReader(
        bars=bars,
        openings=openings,
        instruments=(security(),),
        actions=actions,
        factors=factors,
        knowledge_basis=knowledge,
        scopes={**scopes, **(field_scopes or {})},
        extension_values=extension_values,
    )
    rules = rules or Rules()
    view = StaticPriceView(mode)
    execution = execution or ExecutionModel(timeframe="1d")
    account = CashEquityAccount(
        "CNY", "10000", order_expiry=execution.order_expiry, dividend_treatment=dividend_treatment
    )
    strategy = strategy or ScheduledStrategy()
    reference = DefinitionReference(definition_id="strategy.fixture", version="1", sha256="d" * 64)
    specification = BacktestSpec(
        semantics_version="strict.daily.v1",
        snapshot_id=reader.manifest.snapshot_id,
        universe=("A",),
        start=date(2025, 1, start),
        end=date(2025, 1, end),
        warmup_start=warmup,
        strategy=reference,
        market_profile=rules.reference,
        account_profile=account.reference,
        price_view=PriceViewSpec(
            provider_id=view.provider_id,
            mode=mode,
            **(
                {}
                if mode == "raw"
                else dict(
                    anchor_at=at(anchor, 0),
                    factor_definition_id="cumulative",
                    factor_baseline_id="listing",
                )
            ),
        ),
        execution=execution,
        accounting_model="event_ledger",
        knowledge_requirement="historical_versions",
        initial_capital="10000",
        base_currency="CNY",
    )
    lock = RunLock.create(
        RunLockContents(
            spec=specification,
            snapshot=reader.manifest,
            packages=(
                PackageReference(name="tivelo-backtest", version="0.1.0a1", sha256="b" * 64),
            ),
            runtime=RuntimeReference(
                python_version="3.11.17",
                python_abi="cp311",
                platform_tag="synthetic",
                dependency_lock_sha256="f" * 64,
            ),
            verifier_version=VERIFIER_VERSION,
        )
    )
    return (
        DailyBacktest(rules=rules, price_view=view),
        lock,
        reader,
        StrategyBinding(strategy, reference),
    )


def run(values, **options):
    runner, lock, reader, binding = values
    return runner.run(lock, reader, binding, **options)


def test_preflight_validates_references_without_calling_strategy():
    runner, lock, reader, binding = case()
    days = runner.preflight(lock, reader, strategy=binding.reference)
    assert [day.trading_date.day for day in days] == [2, 3, 6]
    assert binding.implementation.contexts == []
    with pytest.raises(TiveloError):
        runner.preflight(
            lock, reader, strategy=binding.reference.model_copy(update={"sha256": "e" * 64})
        )


def test_independent_daily_flow_hand_calculated_dates_prices_and_cash():
    values = case()
    result = run(values)
    fills = [event for event in result.orders if event.status == "filled"]
    assert [
        (f.decision_at.day, f.filled_at.day, f.filled_quantity, f.raw_fill_price) for f in fills
    ] == [(2, 3, Decimal(1000), Decimal(10)), (3, 6, Decimal(1000), Decimal(11))]
    assert result.final_portfolio.cash[0].total == 11000
    assert result.final_portfolio.positions == ()
    assert [point.equity for point in result.equity] == [10000, 10000, 11000]
    assert result.lock_id == values[1].lock_id
    contexts = values[3].implementation.contexts
    assert [len(c.history) for c in contexts] == [1, 2, 3]
    assert all(b.available_at <= c.now and b.bar_end <= c.now for c in contexts for b in c.history)
    assert not hasattr(contexts[0], "reader")


def test_later_bar_revision_is_visible_only_in_later_strategy_context():
    correction = bar(2, "9", revision_id="2", available_at=at(6, 7), ingested_at=at(6, 7))
    values = case(bars=(bar(2, "10"), bar(3, "10"), bar(6, "11"), correction))
    result = run(values)
    contexts = values[3].implementation.contexts
    assert [c.history[0].close for c in contexts] == [10, 10, 9]
    assert result.final_portfolio.cash[0].total == 11000


def test_late_opening_fact_cannot_be_reconstructed_from_the_final_bar():
    values = case(
        openings=(
            opening(2, "10"),
            opening(3, "10", available_at=at(3, 2), ingested_at=at(3, 2)),
            opening(6, "11"),
        )
    )
    with pytest.raises(TiveloError) as error:
        run(values)
    assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT


def test_explicit_decision_delay_handles_publication_lag_without_same_close_fills():
    bars = tuple(
        bar(
            day,
            price,
            available_at=at(day, 7) + timedelta(seconds=30),
            ingested_at=at(day, 7) + timedelta(seconds=30),
        )
        for day, price in ((2, "10"), (3, "10"), (6, "11"))
    )
    with pytest.raises(TiveloError):
        run(case(bars=bars))
    result = run(
        case(bars=bars, execution=ExecutionModel(timeframe="1d", decision_delay_seconds=60))
    )
    fills = [o for o in result.orders if o.status == "filled"]
    assert fills[0].decision_at == at(2, 7) + timedelta(seconds=60)
    assert result.final_portfolio.cash[0].total == 11000


def test_latest_reconstruction_cannot_claim_strict_historical_execution():
    values = case(knowledge="latest_reconstruction")
    with pytest.raises(TiveloError):
        run(values)
    assert values[3].implementation.contexts == []


def test_scope_metadata_does_not_hide_missing_actual_closing_bars():
    values = case(
        bars=(bar(2, "10"), bar(6, "11")),
        openings=(opening(2, "10"), opening(3, "10"), opening(6, "11")),
    )
    with pytest.raises(TiveloError):
        run(values)


def test_halt_is_explicit_and_never_fills_at_a_stale_open():
    result = run(
        case(
            bars=(bar(2, "10"), bar(6, "11")),
            openings=(opening(2, "10"), opening(3, None, state="suspended"), opening(6, "11")),
        )
    )
    assert not [o for o in result.orders if o.status == "filled"]
    assert result.final_portfolio.cash[0].total == 10000
    assert "opening_suspended" in result.diagnostics
    assert any(
        e.code == "opening_suspended" and e.instrument_id == "A" and e.at == at(3)
        for e in result.diagnostic_events
    )


def test_stale_valuation_needs_explicit_policy_and_is_reported():
    values = dict(
        bars=(bar(2, "10"), bar(3, "10")),
        openings=(opening(2, "10"), opening(3, "10"), opening(6, None, state="suspended")),
    )
    with pytest.raises(TiveloError):
        run(case(**values))
    result = run(
        case(**values, execution=ExecutionModel(timeframe="1d", missing_data="hold_last_valuation"))
    )
    assert result.final_portfolio.positions[0].quantity == 1000
    assert result.equity[-1].equity == 10000
    assert "stale_valuation" in result.diagnostics


def action(**changes):
    values = dict(
        action_id="split",
        instrument_id="A",
        action_type="split",
        quantity_ratio="2",
        effective_at=at(6, 0),
        available_at=at(1, 0),
        source_id="synthetic",
        revision_id="1",
    )
    values.update(changes)
    return CorporateAction(**values)


def factors():
    return tuple(
        AdjustmentFactor(
            factor_id=f"f{day}",
            instrument_id="A",
            value=value,
            effective_at=at(day, 0),
            available_at=at(day, 0),
            factor_kind="cumulative_forward",
            definition_id="cumulative",
            baseline_id="listing",
            source_id="synthetic",
            revision_id="1",
        )
        for day, value in ((1, "2"), (6, "4"))
    )


def test_split_updates_raw_account_and_hfq_strategy_prices_without_double_return():
    values = case(
        bars=(bar(2, "10"), bar(3, "10"), bar(6, "5.5")),
        actions=(action(),),
        factors=factors(),
        mode="hfq",
    )
    result = run(values)
    assert result.final_portfolio.positions[0].quantity == 2000
    assert result.final_portfolio.positions[0].cost == 10000
    assert result.equity[-1].equity == 11000
    assert values[3].implementation.contexts[-1].history[-1].close == 11
    assert [entry.kind for entry in result.ledger] == ["buy", "split"]
    assert any(o.reason == "corporate_action_changed_basis" for o in result.orders)


def test_future_qfq_anchor_is_rejected_before_strategy_decides():
    values = case(actions=(action(),), factors=factors(), mode="qfq", anchor=6)
    with pytest.raises(TiveloError) as error:
        run(values)
    assert error.value.code == ErrorCode.PRICE_BASIS_UNPROVEN
    assert values[3].implementation.contexts == []


def test_dividend_record_ex_and_payment_events_preserve_total_equity():
    dividend = action(
        action_id="dividend",
        action_type="cash_dividend",
        quantity_ratio=None,
        cash_per_unit="1",
        currency="CNY",
        record_at=at(3, 7),
        payment_at=at(7, 0),
    )
    result = run(
        case(
            bars=(bar(2, "10"), bar(3, "10"), bar(6, "9"), bar(7, "9")),
            actions=(dividend,),
            end=7,
            dividend_treatment="gross_before_personal_tax",
        )
    )
    assert [point.equity for point in result.equity] == [10000] * 4
    assert result.final_portfolio.cash[0].total == 1000
    assert result.final_portfolio.cash[0].receivable == 0
    assert [entry.kind for entry in result.ledger] == [
        "buy",
        "dividend_accrual",
        "dividend_payment",
    ]
    assert "gross_dividends_before_personal_tax" in result.diagnostics


def test_warmup_data_never_creates_orders_before_the_tradable_period():
    values = case(start=3, warmup=date(2025, 1, 2), strategy=ScheduledStrategy({3: "buy"}))
    result = run(values)
    assert [c.now.day for c in values[3].implementation.contexts] == [3, 6]
    assert len(values[3].implementation.contexts[0].history) == 2
    assert (
        result.orders[-1].reason == "insufficient_cash"
    )  # Gap to 11 does not resize a 10-based order.
    assert result.final_portfolio.cash[0].total == 10000


def test_fee_reserve_uses_declared_quantity_steps_at_decision_time():
    class CommissionRules(Rules):
        commission = Decimal(5)

    result = run(case(rules=CommissionRules()))
    assert [o.filled_quantity for o in result.orders if o.status == "filled"] == [900, 900]
    assert result.final_portfolio.cash[0].total == 10890


def test_cancellation_cannot_return_partial_success():
    class StopAfterOneDay:
        def __init__(self):
            self.completed = 0

        def cancelled(self):
            return self.completed == 1

        def progress(self, completed, total, stage):
            assert total == 3 and stage == "backtest"
            self.completed = completed

    with pytest.raises(TiveloError) as error:
        run(case(), observer=StopAfterOneDay())
    assert error.value.code == ErrorCode.CANCELLED


def test_end_marks_open_holdings_without_inventing_a_final_sale():
    result = run(case(strategy=ScheduledStrategy({2: "buy"})))
    assert [o.side for o in result.orders if o.status == "filled"] == ["buy"]
    assert result.final_portfolio.positions[0].quantity == 1000
    assert result.equity[-1].equity == 11000


def test_price_extension_cannot_change_share_volume_or_currency():
    class InvalidView(StaticPriceView):
        def project(self, *args, **kwargs):
            return tuple(
                Bar(**{**bar.model_dump(), "volume": "999999"})
                for bar in super().project(*args, **kwargs)
            )

    values = case()
    values[0].price_view = InvalidView("raw")
    with pytest.raises(TiveloError) as error:
        run(values)
    assert error.value.code == ErrorCode.CONTRACT_INVALID


@pytest.mark.parametrize(
    "fault", ["none", "raw_label", "wrong_provider", "changed_volume", "wrong_lock"]
)
def test_pinned_extension_views_are_strategy_inputs_only(fault):
    runner, initial, reader, binding = case(factors=factors())
    cap = "demo.identity"
    requested = PriceViewSpec(mode="extension", provider_id=cap)
    pin = ExtensionPin(
        extension_id="demo.views",
        version="1",
        kind="price_view",
        capability_id=cap,
        archive_sha256="6" * 64,
        entry_sha256="7" * 64,
        api_version=1,
        runtime_generation="fixture",
    )
    lock = RunLock.create(
        initial.contents.model_copy(
            update={
                "spec": initial.contents.spec.model_copy(update={"price_view": requested}),
                "extensions": (pin,),
            }
        )
    )

    class FixtureView:
        provider_id = cap
        extension_reference = pin
        run_lock_id = initial.lock_id if fault == "wrong_lock" else lock.lock_id

        def project(self, bars, factors, spec, *, decision_at):
            if fault == "raw_label":
                return bars
            return tuple(
                ProjectedBar.model_validate(
                    {
                        **b.model_dump(),
                        "source_id": "other" if fault == "wrong_provider" else cap,
                        "price_basis": "extension",
                        "volume": 0 if fault == "changed_volume" else b.volume,
                    }
                )
                for b in bars
            )

        def to_raw(self, value, *, decision_at):
            return value.value

    runner.price_view = FixtureView()
    if fault == "none":
        result = runner.run(lock, reader, binding)
        assert result.equity == run(case(factors=factors())).equity
        assert all(
            isinstance(b, ProjectedBar) for c in binding.implementation.contexts for b in c.history
        )
    else:
        with pytest.raises(TiveloError) as error:
            runner.run(lock, reader, binding)
        assert error.value.code == (
            ErrorCode.DATA_CONFLICT if fault == "wrong_lock" else ErrorCode.CONTRACT_INVALID
        )


def test_stale_valuation_uses_the_latest_known_revision_and_never_pre_split_units():
    correction = bar(3, "12", available_at=at(6, 7), ingested_at=at(6, 7), revision_id="2")
    values = dict(
        bars=(bar(2, "10"), bar(3, "10"), correction),
        openings=(opening(2, "10"), opening(3, "10"), opening(6, None, state="suspended")),
        execution=ExecutionModel(timeframe="1d", missing_data="hold_last_valuation"),
    )
    assert run(case(**values)).equity[-1].equity == 12000
    with pytest.raises(TiveloError):
        run(case(**values, actions=(action(),)))


@pytest.mark.parametrize("kind", ["future_time", "foreign_instrument", "failure"])
def test_invalid_strategy_results_cannot_become_a_successful_run(kind):
    class InvalidStrategy:
        def decide(self, context):
            if kind == "failure":
                raise RuntimeError("private strategy detail")
            moment = context.now + timedelta(seconds=1) if kind == "future_time" else context.now
            return SignalBatch(
                decision_at=moment,
                signals=(
                    Signal(
                        instrument_id="B" if kind == "foreign_instrument" else "A",
                        decision_at=moment,
                        side="buy",
                        target_weight="1",
                        score="1",
                        source_id="strategy.fixture",
                    ),
                ),
            )

    with pytest.raises(TiveloError) as error:
        run(case(strategy=InvalidStrategy()))
    assert error.value.code == (
        ErrorCode.STRATEGY_FAILED if kind == "failure" else ErrorCode.FUTURE_INFORMATION
    )
    assert "private strategy detail" not in str(error.value)


def test_loaded_definitions_and_market_rules_must_match_the_run_lock():
    values = case()
    other = DefinitionReference(definition_id="other", version="1", sha256="e" * 64)
    with pytest.raises(TiveloError) as error:
        values[0].run(values[1], values[2], StrategyBinding(values[3].implementation, other))
    assert error.value.code == ErrorCode.DATA_CONFLICT

    class DifferentRules(Rules):
        reference = other

    values[0].rules = DifferentRules()
    with pytest.raises(TiveloError) as error:
        run(values)
    assert error.value.code == ErrorCode.DATA_CONFLICT


def test_corrected_action_after_record_date_requires_explicit_reconciliation():
    original = action(
        action_id="dividend",
        action_type="cash_dividend",
        quantity_ratio=None,
        cash_per_unit="1",
        currency="CNY",
        record_at=at(3, 7),
        payment_at=at(7, 0),
    )
    corrected = CorporateAction(
        **{
            **original.model_dump(),
            "revision_id": "2",
            "cash_per_unit": "2",
            "available_at": at(6, 0),
        }
    )
    with pytest.raises(TiveloError) as error:
        run(case(actions=(original, corrected), dividend_treatment="gross_before_personal_tax"))
    assert error.value.code == ErrorCode.DATA_CONFLICT
