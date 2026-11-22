"""Cross-package strict accounting with synthetic prices and explicit CN profiles.

Source-backed quantity/fee fixtures are identified at their use sites. No host
services or user market data participate in these numeric fixtures.
"""

import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from tivelo_backtest.account import CashEquityAccount
from tivelo_contracts.data import Instrument
from tivelo_contracts.execution import Signal
from tivelo_contracts.market import ReferencePrice, TradingTerms
from tivelo_contracts.protocols import ExecutionRules
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


def configured_market():
    days = tuple(
        CalendarDay(
            trading_date=date(2025, 1, day),
            is_open=day in (2, 3, 6, 7),
            sessions=(
                SessionInterval(
                    session_id=f"fixture-{day}", kind="continuous", start=at(day), end=at(day, 7)
                ),
            )
            if day in (2, 3, 6, 7)
            else (),
            closed_reason=None if day in (2, 3, 6, 7) else "synthetic-closed",
        )
        for day in range(1, 8)
    )
    calendar = CNCalendar(
        CalendarVersion.create(
            CalendarContents(
                calendar_id="fixture",
                venue_id="XSHG",
                source_id="synthetic",
                source_sha256="a" * 64,
                available_at=at(1, 0),
                days=days,
                bar_convention_id="fixture.daily",
            )
        )
    )
    historical = dict(
        valid_from=at(1, 0), available_at=at(1, 0), revision_id="1", source_id="synthetic"
    )
    terms = TradingTerms(
        spec_revision="fixture",
        market_id="cn.equity",
        venue_id="XSHG",
        asset_class="equity",
        instrument_type="common_stock",
        quantity_unit="share",
        currency="CNY",
        price_tick="0.01",
        buy_minimum="200",
        buy_increment="1",
        buy_maximum="100000",
        sell_minimum="200",
        sell_increment="1",
        sell_maximum="100000",
        odd_lot_policy="all_remaining",
        sell_lag_sessions=1,
        price_limit_mode="relative_reference",
        limit_up_ratio="0.1",
        limit_down_ratio="0.1",
        source_reference="synthetic-only",
        **historical,
    )
    fees = tuple(
        FeeRule(
            fee_id=key,
            side=side,
            rate=rate,
            minimum=minimum,
            quantum="0.01",
            rounding="half_up",
            source_reference="synthetic-only",
            **historical,
        )
        for key, side, rate, minimum in (
            ("commission", "both", "0.0002", "5"),
            ("stamp", "sell", "0.0005", "0"),
            ("transfer", "both", "0.00001", "0"),
        )
    )
    rules = CNTradingRules(
        RuleConfiguration(
            profile_id="cn.synthetic",
            profile_version="1",
            currency="CNY",
            terms=(terms,),
            required_fee_ids=("commission", "stamp", "transfer"),
            fee_rules=fees,
        ),
        calendar,
    )
    security = Instrument(
        instrument_id="A",
        venue_id="XSHG",
        market_id="cn.equity",
        asset_class="equity",
        instrument_type="common_stock",
        display_symbol="A",
        name="Synthetic",
        quote_currency="CNY",
        settlement_currency="CNY",
        calendar_id="fixture",
        timezone="Asia/Shanghai",
        spec_revision="fixture",
        listing_date=dict(state="known", value="2020-01-01"),
        delisting_date=dict(state="not_yet"),
        tradable=True,
        **historical,
    )
    return rules, security


@pytest.fixture
def market():
    return configured_market()


def configured_order_types(market):
    from importlib.metadata import entry_points

    from tivelo_contracts.market import OrderQuantityTerms
    from tivelo_market_cn.adapter import CNMarketProfile

    rules, security = market
    (term,) = rules.configuration.terms
    specific = tuple(
        OrderQuantityTerms(
            order_type=kind,
            buy_minimum="200",
            buy_increment="1",
            buy_maximum=maximum,
            sell_minimum="200",
            sell_increment="1",
            sell_maximum=maximum,
            odd_lot_policy="all_remaining",
        )
        for kind, maximum in (("limit", "100000"), ("market", "50000"))
    )
    configuration = rules.configuration.model_copy(
        update={
            "schema_version": "1.1",
            "terms": (term.model_copy(update={"order_quantity_rules": specific}),),
        }
    )
    profile = CNMarketProfile(configuration=configuration, calendar=rules.calendar.version)
    (factory,) = entry_points(group="tivelo.market_rules.v1", name="tivelo.market.cn.cash-equity")
    return factory.load()(configuration_json=profile.model_dump_json()), security


def configured_rule_history(market):
    """Synthetic classifications, rule epochs and fees with independent clocks."""
    from tivelo_market_cn.adapter import CNMarketProfile, create_rules
    from tivelo_market_cn.classification import CNEquityClass

    rules, security = configured_order_types(market)
    normal = CNEquityClass(
        venue_id="XSHG", board="main", risk_status="normal", listing_phase="regular"
    )
    warning = normal.model_copy(update={"risk_status": "risk_warning"})
    first = normal.bind(security)
    ended = first.model_copy(
        update={"valid_to": at(3, 0), "available_at": at(2, 12), "revision_id": "normal-ended"}
    )
    changed = warning.bind(
        security.model_copy(
            update={"valid_from": at(3, 0), "available_at": at(2, 12), "revision_id": "warning"}
        )
    )
    (term,) = rules.configuration.terms
    normal_term = term.model_copy(update={"spec_revision": normal.spec_revision})
    warning_term = term.model_copy(
        update={
            "spec_revision": warning.spec_revision,
            "limit_up_ratio": Decimal("0.05"),
            "limit_down_ratio": Decimal("0.05"),
            "revision_id": "warning-original",
        }
    )
    changed_term = warning_term.model_copy(
        update={
            "valid_from": at(6, 0),
            "available_at": at(2, 12),
            "revision_id": "warning-next",
            "limit_up_ratio": Decimal("0.1"),
            "limit_down_ratio": Decimal("0.1"),
        }
    )
    commission = next(fee for fee in rules.configuration.fee_rules if fee.fee_id == "commission")
    next_commission = commission.model_copy(
        update={
            "valid_from": at(6, 0),
            "available_at": at(2, 12),
            "rate": Decimal("0.0003"),
            "revision_id": "commission-next",
        }
    )
    configuration = rules.configuration.model_copy(
        update={
            "schema_version": "1.2",
            "terms": (normal_term, warning_term, changed_term),
            "instrument_classes": (normal, warning),
            "fee_rules": (*rules.configuration.fee_rules, next_commission),
        }
    )
    profile = CNMarketProfile(configuration=configuration, calendar=rules.calendar.version)
    return create_rules(configuration_json=profile.model_dump_json()), (first, ended, changed)


def configured_continuous_market(market, *, end=None, archived_profile=False):
    from tivelo_market_cn.adapter import CNMarketProfile, create_rules
    from tivelo_market_cn.order_prices import continuous_limit_reference
    from tivelo_market_cn.presets import compile_calendar, load_calendar_preset
    from tivelo_market_cn.price_presets import compile_price_terms, load_price_preset
    from tivelo_market_cn.quantity_presets import load_quantity_preset, resolve_quantity_rules

    prior, (security, _, _) = configured_rule_history(market)
    state = prior.configuration.instrument_classes[0]
    day, event = date(2025, 1, 3), at(3, 2)
    quantity_terms = resolve_quantity_rules(
        load_quantity_preset(version="2026.1.0"), state, at=event
    )
    base = prior.configuration.terms[0].model_copy(
        update={"valid_to": at(7, 0), "order_quantity_rules": quantity_terms.order_quantities}
    )
    prices = compile_price_terms(load_price_preset(version="2026.1.0"), state, (base,))
    fees = compiled_fee_policy(("handling", "regulatory"))
    calendar = compile_calendar(
        load_calendar_preset(version="2026.1.0"),
        venue_id="XSHG",
        start=day,
        end=end or day,
    )
    profile = CNMarketProfile(
        profile_format="tivelo-cn-profile-v4",
        calendar=calendar,
        fee_compilation=fees,
        price_compilations=(prices,),
        continuous_limit=continuous_limit_reference(version="2026.1.0"),
        configuration=prior.configuration.model_copy(
            update={
                "schema_version": "1.3",
                "instrument_classes": (state,),
                "terms": prices.terms,
                "required_fee_ids": fees.required_fee_ids,
                "fee_rules": fees.fee_rules,
            }
        ),
    )
    rules = create_rules(configuration_json=profile.model_dump_json())
    security = security.model_copy(update={"calendar_id": calendar.contents.calendar_id})
    return (profile if archived_profile else rules), security, calendar


def test_pinned_order_book_replays_without_late_prices(market, tmp_path):
    from tivelo_backtest.continuous import ContinuousLimitAdmission
    from tivelo_backtest.price_views import StaticPriceView
    from tivelo_backtest.snapshot import PortableSnapshotReader
    from tivelo_contracts.base import canonical_bytes
    from tivelo_contracts.data import Bar, SnapshotManifest
    from tivelo_contracts.errors import ErrorCode, TiveloError
    from tivelo_contracts.execution import PriceViewSpec
    from tivelo_contracts.order_books import OrderBookObservation, OrderBookQuery
    from tivelo_contracts.testing import InMemoryReader
    from tivelo_data import DataEngine
    from tivelo_data.providers import SyncRequest
    from tivelo_market_cn.adapter import create_rules

    profile, security, calendar = configured_continuous_market(market, archived_profile=True)
    rules = create_rules(configuration_json=profile.model_dump_json())
    observed_rules = create_rules(
        configuration_json=profile.model_copy(
            update={
                "profile_format": "tivelo-cn-profile-v5",
                "observed_book_max_age_ms": 61_000,
            }
        ).model_dump_json()
    )
    day, event = date(2025, 1, 3), at(3, 2)
    session = next(s for s in calendar.contents.days[0].sessions if s.start <= event < s.end)
    reference = ReferencePrice(
        instrument_id=security.instrument_id,
        trading_date=day,
        value="2",
        kind="exchange_ex_rights",
        available_at=at(3, 1),
        source_id="synthetic-exchange",
        revision_id="prev-1",
    )
    book = OrderBookObservation(
        instrument_id=security.instrument_id,
        event_at=event,
        available_at=event,
        ingested_at=at(4, 0),
        trading_date=day,
        session_id=session.session_id,
        source_id="synthetic-book",
        revision_id="book-1",
        state="continuous",
        price_basis="raw",
        quote_currency="CNY",
        best_bid="1.99",
        best_ask="2.01",
        last_trade="2",
        last_trade_at=event - timedelta(seconds=1),
    )
    later = event + timedelta(minutes=1)
    corrected = book.model_copy(
        update={
            "available_at": later,
            "revision_id": "book-corrected",
            "best_ask": Decimal("2.02"),
        }
    )
    delayed = book.model_copy(
        update={
            "event_at": later,
            "available_at": later + timedelta(seconds=1),
            "revision_id": "late-book",
        }
    )
    halted = delayed.model_copy(
        update={
            "event_at": later + timedelta(seconds=2),
            "available_at": later + timedelta(seconds=2),
            "revision_id": "halt",
            "state": "halted",
        }
    )
    scope = SyncRequest(
        dataset_id="cn-book",
        record_type="order_book_observation",
        market_id="cn.equity",
        asset_class="equity",
        instrument_ids=(security.instrument_id,),
        start=event,
        end=later + timedelta(minutes=1),
    )
    root, source, request = (tmp_path / name for name in ("data", "books.jsonl", "capture.json"))
    request.write_bytes(canonical_bytes(scope))

    def cli(*arguments, error=None):
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "tivelo_data.cli",
                "--root",
                str(root),
                *map(str, arguments),
            ],
            text=True,
            capture_output=True,
        )
        if error is not None:
            assert completed.returncode == 1
            assert json.loads(completed.stderr)["code"] == error.value
            return None
        assert completed.returncode == 0, completed.stderr
        return completed.stdout

    def publish(records, provenance="historical_versions", error=None):
        source.write_bytes(b"\n".join(canonical_bytes(row) for row in records) + b"\n")
        output = cli(
            "import-jsonl",
            "--request",
            request,
            "--input",
            source,
            "--source-id",
            book.source_id,
            "--knowledge-basis",
            provenance,
            error=error,
        )
        return SnapshotManifest.model_validate_json(output) if output is not None else None

    before = publish((book,))
    revised = publish((book, corrected, delayed, halted))
    (dataset,) = revised.contents.datasets
    assert before.snapshot_id != revised.snapshot_id
    audit = OrderBookQuery(
        dataset=dataset,
        instrument_ids=scope.instrument_ids,
        start=event,
        end=event + timedelta(microseconds=1),
        as_of=later,
    )
    audit_path = tmp_path / "audit.json"
    audit_path.write_bytes(canonical_bytes(audit))
    assert (
        OrderBookObservation.model_validate_json(
            cli(
                "query",
                "--snapshot",
                revised.snapshot_id,
                "--record-type",
                "order_book_observation",
                "--request",
                audit_path,
            )
        )
        == corrected
    )
    bundle = tmp_path / "portable-books"
    cli("export", "--snapshot", revised.snapshot_id, "--output", bundle)

    def check(reader, when=event, selected=dataset, engine=rules):
        return engine.check_snapshot_limit_price(
            security,
            "buy",
            Decimal("2.12"),
            at=when,
            reader=reader,
            dataset=selected,
            reference=reference,
        )

    engine = DataEngine(root)
    with (
        engine.open_reader(revised.snapshot_id) as source_reader,
        PortableSnapshotReader(bundle) as portable,
    ):
        assert source_reader.order_books(audit) == portable.order_books(audit) == (corrected,)
        receipt = check(source_reader)
        assert receipt == check(portable)
        assert receipt.snapshot_id == revised.snapshot_id and receipt.dataset == dataset
        assert receipt.check.book_revision == "book-1"
        assert receipt.check.cage_boundary == Decimal("2.11")
        assert receipt.check.result == "outside_price_cage"
        assert portable.prepare(max_bytes=1_000_000) > 0
        assert check(portable) == receipt
        for reader in (source_reader, portable):
            sample = check(reader, later - timedelta(microseconds=1), engine=observed_rules)
            assert sample.check.book_revision == "book-1"
            assert sample.check.book_event_at == sample.check.book_available_at == event
            sample = check(reader, later, engine=observed_rules)
            assert sample.check.book_revision == "book-corrected"
            assert sample.check.book_event_at == event
            assert sample.check.book_available_at == later
            sample = check(reader, delayed.available_at, engine=observed_rules)
            assert sample.check.book_revision == "late-book"
            assert sample.check.book_event_at == later
            assert sample.check.book_available_at == delayed.available_at
            with pytest.raises(TiveloError) as error:
                check(reader, halted.available_at, engine=observed_rules)
            assert error.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
            with pytest.raises(TiveloError) as error:
                check(reader, later)
            assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT

        def run_admission(reader):
            account = CashEquityAccount("CNY", "225", order_expiry="end_of_day")
            admission = ContinuousLimitAdmission(account, rules, reader=reader, dataset=dataset)
            view = StaticPriceView("raw")

            def submit(
                price, quantity=100, *, when=event, sequence=1, side="buy", target=admission
            ):
                decision = event - timedelta(seconds=sequence)
                history = Bar(
                    instrument_id=security.instrument_id,
                    timeframe="1m",
                    bar_start=decision - timedelta(minutes=1),
                    bar_end=decision,
                    trading_date=day,
                    session_id=session.session_id,
                    open="2",
                    high="2",
                    low="2",
                    close="2",
                    volume="100",
                    quantity_unit="share",
                    quote_currency="CNY",
                    price_basis="raw",
                    available_at=decision,
                    ingested_at=decision,
                    source_id="synthetic-bars",
                    revision_id="bar-1",
                )
                view.project(
                    (history,),
                    (),
                    PriceViewSpec(provider_id=view.provider_id, mode="raw"),
                    decision_at=decision,
                )
                signal = Signal(
                    instrument_id=security.instrument_id,
                    decision_at=decision,
                    side=side,
                    score="1",
                    source_id="strategy.fixture",
                    limit_price=view.price(
                        price, instrument_id=security.instrument_id, decision_at=decision
                    ),
                )
                return target.submit(
                    signal,
                    Decimal(quantity),
                    at=when,
                    instrument=security,
                    reference=reference,
                    price_view=view,
                )

            exceeds = submit("2.12")
            assert (exceeds.event.status, exceeds.event.reason) == (
                "rejected",
                "outside_price_cage",
            )
            assert exceeds.price_check == receipt
            assert exceeds.event.decision_at < exceeds.event.submitted_at == event
            assert account.snapshot(event).cash[0].available == 225
            accepted = submit("2.11", sequence=2)
            assert accepted.event.status == "accepted"
            reserved = (
                Decimal("211")
                + rules.execution_rules.fee_breakdown(
                    security,
                    "buy",
                    Decimal(100),
                    Decimal("2.11"),
                    at=event,
                ).total
            )
            assert account.snapshot(event).cash[0].available == Decimal(225) - reserved
            assert submit("2.11", sequence=2) == accepted
            with pytest.raises(TiveloError) as error:
                submit("2.11", 200, sequence=2)
            assert error.value.code == ErrorCode.DATA_CONFLICT
            assert submit("2.11", sequence=3).event.reason == "insufficient_cash"
            assert submit("2.11", 99, sequence=4).event.reason == "invalid_quantity"
            assert (
                submit("1.99", sequence=5, side="sell").event.reason
                == "insufficient_sellable_quantity"
            )
            before = account.orders, account.snapshot(event)
            with pytest.raises(TiveloError) as error:
                submit("2.11", when=later, sequence=6)
            assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
            assert (account.orders, account.snapshot(event)) == before
            with pytest.raises(TiveloError) as error:
                account.match_open(
                    at=event,
                    instruments={security.instrument_id: security},
                    raw_prices={security.instrument_id: Decimal("2")},
                    references={security.instrument_id: reference},
                    rules=rules.execution_rules,
                )
            assert error.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
            assert account.cancel_order(accepted.event.order_id, at=event + timedelta(seconds=1))
            assert submit("2.11", sequence=2) == accepted
            assert not account.cancel_order(accepted.event.order_id, at=later)
            assert account.snapshot(later).cash[0].available == 225
            assert account.snapshot(later).positions == () and not account.ledger

            missing = CashEquityAccount("CNY", "225", order_expiry="end_of_day")
            expiry_admission = ContinuousLimitAdmission(
                missing, rules, reader=reader, dataset=dataset
            )
            open_order = submit("2.11", target=expiry_admission)
            assert open_order.event.status == "accepted"
            closing = calendar.contents.days[0].sessions[-1].end
            assert missing.snapshot(closing - timedelta(microseconds=1)).cash[0].available < 225
            assert missing.snapshot(closing).cash[0].available == 225
            assert not missing.cancel_order(open_order.event.order_id, at=closing)
            assert missing.orders[-1].status == "expired"
            assert missing.orders[-1].event_at == closing
            assert not missing.ledger
            return account.orders, missing.orders

        assert run_admission(source_reader) == run_admission(portable)
    (old_dataset,) = before.contents.datasets
    with engine.open_reader(before.snapshot_id) as original:
        assert check(original, selected=old_dataset).check == receipt.check
        with pytest.raises(TiveloError) as error:
            check(original)
        assert error.value.code == ErrorCode.DATA_CONFLICT

    with engine.open_reader(revised.snapshot_id) as reader:
        capture_scope = reader.datasets[0].contents.scope
    memory = InMemoryReader(
        order_books=(book, corrected, delayed),
        knowledge_basis="historical_versions",
        scopes={"order_book_observation": capture_scope},
    )
    (memory_dataset,) = memory.manifest.contents.datasets
    assert memory.order_books(audit.model_copy(update={"dataset": memory_dataset})) == (corrected,)
    assert check(memory, selected=memory_dataset).check == receipt.check

    publish(
        (book.model_copy(update={"event_at": event - timedelta(seconds=1)}),),
        error=ErrorCode.CONTRACT_INVALID,
    )
    publish(
        (book.model_copy(update={"best_bid": book.best_ask}),), error=ErrorCode.CONTRACT_INVALID
    )
    publish(
        (book, corrected.model_copy(update={"available_at": event})), error=ErrorCode.DATA_CONFLICT
    )
    publish((book,), provenance="latest_reconstruction", error=ErrorCode.FUTURE_INFORMATION)
    observed = book.model_copy(update={"available_at": book.ingested_at})
    reconstructed = publish((observed,), provenance="latest_reconstruction")
    with engine.open_reader(reconstructed.snapshot_id) as hindsight:
        with pytest.raises(TiveloError) as error:
            check(hindsight, selected=reconstructed.contents.datasets[0])
        assert error.value.code == ErrorCode.FUTURE_INFORMATION


def test_locked_trade_prints_drive_partial_fills_and_t_plus_one(market, tmp_path, monkeypatch):
    from tivelo_backtest.continuous import ContinuousLimitAdmission, ContinuousTradeReplay
    from tivelo_backtest.price_views import StaticPriceView
    from tivelo_backtest.snapshot import PortableSnapshotReader
    from tivelo_contracts.base import canonical_bytes
    from tivelo_contracts.data import Bar, CorporateAction, SnapshotManifest
    from tivelo_contracts.errors import ErrorCode, TiveloError
    from tivelo_contracts.execution import PriceViewSpec
    from tivelo_contracts.order_books import OrderBookObservation
    from tivelo_contracts.testing import InMemoryReader
    from tivelo_contracts.trade_prints import TradePrint, TradePrintQuery
    from tivelo_data import DataEngine
    from tivelo_data.providers import SyncRequest

    rules, instrument, _ = configured_continuous_market(market, end=date(2025, 1, 6))
    key, start, tomorrow = instrument.instrument_id, at(3, 2), at(6, 2)
    first, equal, late, second, ample = (start + timedelta(seconds=i) for i in range(1, 6))
    close = rules.cash_limit_deadline(instrument, "buy", at=start)
    closing_print = close - timedelta(minutes=5)
    next_print = closing_print + timedelta(seconds=30)
    tail_print = closing_print + timedelta(minutes=1)
    tail_confirmed = tomorrow + timedelta(milliseconds=250)
    exit_at = tomorrow + timedelta(seconds=1)
    root = tmp_path / "store"

    def day_reference(moment):
        return ReferencePrice(
            instrument_id=key,
            trading_date=moment.date(),
            value="2",
            kind="exchange_ex_rights",
            available_at=moment.replace(hour=1, second=0),
            source_id="exchange",
            revision_id=moment.date().isoformat(),
        )

    def witness(moment, *, source, **fields):
        return dict(
            instrument_id=key,
            event_at=moment,
            trading_date=moment.date(),
            session_id=rules.continuous_session_id(moment),
            quote_currency="CNY",
            available_at=fields.pop("available_at", moment),
            ingested_at=at(7, 0),
            price_basis="raw",
            source_id=source,
            revision_id="original",
            **fields,
        )

    def trade(moment, sequence, quantity, *, price="2", side="sell", **fields):
        return TradePrint(
            **witness(
                moment,
                source="trades",
                sequence=sequence,
                quantity=quantity,
                quantity_unit="share",
                raw_price=price,
                aggressor_side=side,
                **fields,
            )
        )

    original = trade(first, 2, 60)
    correction = original.model_copy(
        update={
            "event_at": late,
            "available_at": second,
            "quantity": Decimal(160),
            "revision_id": "correction",
        }
    )
    tape = (
        trade(start, 1, 150),
        original,
        correction,
        trade(equal, 3, 90, price="2.11"),
        trade(late, 4, 60, available_at=second),
        trade(second, 5, 40),
        trade(ample, 6, 150),
        trade(ample + timedelta(seconds=1), 7, 100, side="unknown"),
        trade(closing_print, 8, 60, available_at=tomorrow),
        trade(next_print, 12, 25, available_at=tail_print),
        trade(next_print, 12, 20, available_at=tomorrow).model_copy(
            update={"revision_id": "corrected-before-delivery"}
        ),
        trade(tail_print, 19, 10, available_at=tail_confirmed),
        trade(tail_print, 19, 35, available_at=exit_at).model_copy(
            update={"revision_id": "corrected-after-delivery"}
        ),
        trade(exit_at, 1, 100, price="2.05", side="buy"),
    )
    books = tuple(
        OrderBookObservation(
            **witness(
                moment,
                source="books",
                state="continuous",
                best_bid="1.99",
                best_ask="2.10" if moment == equal else "2.01",
                last_trade=None,
            )
        )
        for moment in (start, first, equal, second, tomorrow)
    )

    def cli(*arguments):
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "tivelo_data.cli",
                "--root",
                str(root),
                *map(str, arguments),
            ],
            text=True,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stderr
        return completed.stdout

    def publish(dataset_id, kind, records):
        req, rows = (tmp_path / (dataset_id + suffix) for suffix in (".json", ".jsonl"))
        req.write_bytes(
            canonical_bytes(
                SyncRequest(
                    dataset_id=dataset_id,
                    record_type=kind,
                    market_id="cn.equity",
                    asset_class="equity",
                    instrument_ids=(key,),
                    start=start,
                    end=exit_at + timedelta(seconds=1),
                )
            )
        )
        rows.write_bytes(b"\n".join(map(canonical_bytes, records)) + b"\n")
        (version,) = SnapshotManifest.model_validate_json(
            cli(
                "import-jsonl",
                "--request",
                req,
                "--input",
                rows,
                "--source-id",
                dataset_id,
                "--knowledge-basis",
                "historical_versions",
            )
        ).contents.datasets
        return version

    book_ref = publish("books", "order_book_observation", books)
    trade_ref = publish("trades", "trade_print", tape)
    engine = DataEngine(root)
    snapshot = engine.create_snapshot((book_ref.version_id, trade_ref.version_id))
    bundle = tmp_path / "portable"
    cli("export", "--snapshot", snapshot.snapshot_id, "--output", bundle)
    audit = TradePrintQuery(
        dataset=trade_ref,
        trading_date=start.date(),
        instrument_ids=(key,),
        sequence=2,
        start=first,
        end=first + timedelta(microseconds=1),
        as_of=second,
    )
    request = tmp_path / "trade-query.json"
    request.write_bytes(canonical_bytes(audit))
    assert (
        cli(
            "query",
            "--snapshot",
            snapshot.snapshot_id,
            "--record-type",
            "trade_print",
            "--request",
            request,
        )
        == ""
    )

    def play(reader):
        assert reader.trade_prints(audit) == ()
        query = audit.model_copy(update={"as_of": first})
        assert reader.trade_prints(query) == (original,)

        def context(capital, *, expiry="end_of_day"):
            account = CashEquityAccount("CNY", capital, order_expiry=expiry)
            replay = ContinuousLimitAdmission(
                account, rules, reader=reader, dataset=book_ref, trade_dataset=trade_ref
            )

            def submit(moment, side="buy", limit="2.11", delay=1):
                decision = moment - timedelta(milliseconds=delay)
                bar = Bar(
                    instrument_id=key,
                    timeframe="1m",
                    bar_start=decision - timedelta(minutes=1),
                    bar_end=decision,
                    trading_date=decision.date(),
                    session_id="fixture",
                    open="2",
                    high="2",
                    low="2",
                    close="2",
                    volume="100",
                    quantity_unit="share",
                    quote_currency="CNY",
                    price_basis="raw",
                    available_at=decision,
                    ingested_at=decision,
                    source_id="bars",
                    revision_id="1",
                )
                view = StaticPriceView("raw")
                view.project(
                    (bar,),
                    (),
                    PriceViewSpec(provider_id=view.provider_id, mode="raw"),
                    decision_at=decision,
                )
                signal = Signal(
                    instrument_id=key,
                    decision_at=decision,
                    side=side,
                    score="1",
                    source_id="fixture",
                    limit_price=view.price(limit, instrument_id=key, decision_at=decision),
                )
                return replay.submit(
                    signal,
                    Decimal(100),
                    at=moment,
                    instrument=instrument,
                    reference=day_reference(moment),
                    price_view=view,
                ).event

            def match(moment, sequence, **options):
                return replay.match_trade(
                    at=moment,
                    sequence=sequence,
                    instrument=instrument,
                    reference=options.pop("reference", day_reference(moment)),
                    **options,
                )

            return account, submit, match

        account, submit, match = context("225")
        assert submit(start).status == "accepted"
        assert not match(start, 1).fills
        partial = match(first, 2)
        assert match(first, 2) == partial and partial.trade == original
        (fill,) = partial.fills
        assert (fill.status, fill.filled_quantity, fill.raw_fill_price, fill.fee) == (
            "partially_filled",
            60,
            Decimal("2.11"),
            5,
        )
        state = account.snapshot(first)
        assert (state.cash[0].total, state.cash[0].available) == (Decimal("93.4"), 4)
        assert (state.positions[0].quantity, state.positions[0].sellable_quantity) == (60, 0)
        assert submit(first, "sell", "2").reason == "insufficient_sellable_quantity"
        assert not match(equal, 3).fills
        with pytest.raises(TiveloError) as error:
            match(late, 4)
        assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
        assert account.snapshot(equal) == state
        filled = match(second, 5)
        assert filled.fills[0].status == "filled" and filled.fills[0].filled_quantity == 100
        assert account.snapshot(second).cash[0].total == 4
        with pytest.raises(TiveloError) as error:
            match(first, 2)
        assert error.value.code == ErrorCode.FUTURE_INFORMATION
        assert account.snapshot(tomorrow).positions[0].sellable_quantity == 100
        assert submit(tomorrow, "sell", "2").status == "accepted"
        assert account.snapshot(tomorrow).positions[0].sellable_quantity == 0
        sold = match(exit_at, 1)
        assert sold.fills[0].status == "filled" and sold.fills[0].raw_fill_price == 2
        assert account.snapshot(exit_at).positions == ()
        assert [entry.quantity_delta for entry in account.ledger] == [60, 40, -100]

        strict_cash, submit, match = context("216")
        submit(start)
        assert not match(first, 2).fills and strict_cash.snapshot(first).cash[0].available == 0
        assert match(ample, 6).fills[0].filled_quantity == 100
        with pytest.raises(TiveloError) as error:
            match(ample + timedelta(seconds=1), 7)
        assert error.value.code == ErrorCode.UNSUPPORTED_CAPABILITY

        locked, submit, match = context("300")
        assert submit(equal, limit="2.2").status == "accepted"
        assert not match(second, 5).fills and not locked.ledger

        cancelled, submit, match = context("225")
        ticket = submit(start)
        match(first, 2)
        assert cancelled.cancel_order(ticket.order_id, at=equal)
        cash = cancelled.snapshot(equal).cash[0]
        assert cash.total == cash.available == Decimal("93.4")
        assert cancelled.ledger[0].quantity_delta == 60

        crowded, submit, match = context("700")
        submit(start, limit="2.09", delay=2)
        higher = submit(start)
        same_price = submit(start, delay=3)
        freeze = crowded.snapshot(start)
        sellable = rules.execution_rules.sellable_at
        calls = 0

        def bad_settlement(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("second allocation failed")
            return sellable(*args)

        with monkeypatch.context() as patch:
            patch.setattr(rules.execution_rules, "sellable_at", bad_settlement)
            with pytest.raises(RuntimeError, match="second allocation failed"):
                match(ample, 6)
        assert not crowded.ledger and len(crowded.orders) == 6
        assert crowded.snapshot(start) == freeze
        filled = match(ample, 6).fills
        assert [(f.order_id, f.filled_quantity) for f in filled] == [
            (higher.order_id, 100),
            (same_price.order_id, 50),
        ]

        delayed, submit, match = context("500")
        earlier = submit(start, limit="2.09")
        with pytest.raises(TiveloError) as error:
            match(late, 4, confirmed_at=late + timedelta(milliseconds=500))
        assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
        later = submit(second)
        assert later.status == "accepted"
        pending = delayed.snapshot(second)
        assert not pending.positions and not delayed.ledger
        with pytest.raises(TiveloError) as error:
            match(late, 4, confirmed_at=equal)
        assert error.value.code == ErrorCode.FUTURE_INFORMATION
        with pytest.raises(TiveloError) as error:
            match(late, 4, confirmed_at=late + timedelta(milliseconds=500))
        assert error.value.code == ErrorCode.FUTURE_INFORMATION  # Account is already at second.
        with pytest.raises(TiveloError) as error:
            match(
                late,
                4,
                confirmed_at=second,
                reference=day_reference(late).model_copy(update={"available_at": second}),
            )
        assert error.value.code == ErrorCode.FUTURE_INFORMATION
        assert delayed.snapshot(second) == pending
        report = match(late, 4, confirmed_at=second)
        (event,) = report.fills
        assert (event.order_id, event.filled_at, event.event_at, report.confirmed_at) == (
            earlier.order_id,
            late,
            second,
            second,
        )
        assert delayed.ledger[0].at == second
        position = delayed.snapshot(second).positions[0]
        assert (position.quantity, position.sellable_quantity) == (60, 0)
        assert delayed.cancel_order(later.order_id, at=ample)
        assert match(late, 4, confirmed_at=second) == report
        with pytest.raises(TiveloError) as error:
            match(late, 4, confirmed_at=ample)
        assert error.value.code == ErrorCode.DATA_CONFLICT

        conflict, submit, match = context("500")
        cancelled_first = submit(start)
        remaining = submit(first, limit="2.09")
        assert conflict.cancel_order(
            cancelled_first.order_id, at=late + timedelta(milliseconds=500)
        )
        before = conflict.orders, conflict.snapshot(second)
        with pytest.raises(TiveloError) as error:
            match(late, 4, confirmed_at=second)
        assert error.value.code == ErrorCode.DATA_CONFLICT
        assert (conflict.orders, conflict.snapshot(second)) == before and not conflict.ledger
        assert match(second, 5).fills[0].order_id == remaining.order_id

        cash_credit, submit, match = context("421")
        submit(start)
        refund = submit(first, limit="2")
        assert cash_credit.snapshot(late).cash[0].available == 0
        assert cash_credit.cancel_order(refund.order_id, at=late + timedelta(milliseconds=500))
        with pytest.raises(TiveloError) as error:
            match(late, 4, confirmed_at=second)
        assert error.value.code == ErrorCode.DATA_CONFLICT and not cash_credit.ledger

        cash_hold, submit, match = context("432")
        submit(start)
        assert submit(second).status == "accepted"
        assert cash_hold.snapshot(second).cash[0].available == 0
        with pytest.raises(TiveloError) as error:
            match(late, 4, confirmed_at=second)
        assert error.value.code == ErrorCode.DATA_CONFLICT and not cash_hold.ledger

        removed, submit, match = context("225")
        ticket = submit(start)
        assert removed.cancel_order(ticket.order_id, at=equal)
        assert not match(late, 4, confirmed_at=second).fills and not removed.ledger

        auto, submit, match = context("225")
        submit(start)
        with pytest.raises(TiveloError) as error:
            match(closing_print, 8, confirmed_at=tomorrow)
        assert error.value.code == ErrorCode.DATA_CONFLICT
        assert not auto.ledger

        ownership, submit, match = context("225", expiry="confirmed_end_of_day")
        submit(start)
        ownership.capture_entitlement(
            CorporateAction(
                action_id="dividend",
                instrument_id=key,
                action_type="cash_dividend",
                currency="CNY",
                cash_per_unit="1",
                source_id="fixture",
                revision_id="1",
                available_at=start,
                record_at=close,
                effective_at=tomorrow,
                payment_at=at(7, 0),
            ),
            at=close,
        )
        with pytest.raises(TiveloError) as error:
            match(closing_print, 8, confirmed_at=tomorrow)
        assert error.value.code == ErrorCode.DATA_CONFLICT and not ownership.ledger

        overnight, submit, match = context("225", expiry="confirmed_end_of_day")
        ticket = submit(start)
        assert overnight.snapshot(close).cash[0].available == 9
        with pytest.raises(TiveloError) as error:
            overnight.confirm_expiry(ticket.order_id, at=closing_print)
        assert error.value.code == ErrorCode.CONTRACT_INVALID
        assert submit(tomorrow).reason == "insufficient_cash"
        assert not overnight.snapshot(tomorrow).positions
        assert not overnight.cancel_order(ticket.order_id, at=tomorrow)
        with pytest.raises(TiveloError) as error:
            overnight.finish(tomorrow)
        assert error.value.code == ErrorCode.DATA_CONFLICT
        overnight_report = match(closing_print, 8, confirmed_at=tomorrow)
        (event,) = overnight_report.fills
        assert (event.filled_at, event.event_at, event.filled_quantity) == (
            closing_print,
            tomorrow,
            60,
        )
        unsettled = overnight.snapshot(tomorrow)
        assert unsettled.cash[0].available == 4 and unsettled.positions[0].sellable_quantity == 60
        assert overnight.confirm_expiry(ticket.order_id, at=tomorrow)
        assert not overnight.confirm_expiry(ticket.order_id, at=tomorrow)
        assert overnight.snapshot(tomorrow).cash[0].available == Decimal("93.4")
        assert (
            overnight.orders[-1].status == "expired" and overnight.orders[-1].event_at == tomorrow
        )
        assert overnight.ledger[0].at == tomorrow
        assert match(closing_print, 8, confirmed_at=tomorrow) == overnight_report
        overnight.finish(tomorrow)

        def driver(account, sequences):
            admission = ContinuousLimitAdmission(
                account, rules, reader=reader, dataset=book_ref, trade_dataset=trade_ref
            )
            query = TradePrintQuery(
                dataset=trade_ref,
                trading_date=start.date(),
                instrument_ids=(key,),
                start=closing_print,
                end=close,
                as_of=closing_print,
            )
            return ContinuousTradeReplay(
                admission,
                query,
                sequences=sequences,
                instrument=instrument,
                reference=day_reference(start),
            )

        queued, submit, _ = context("235", expiry="confirmed_end_of_day")
        ticket = submit(start)
        replay = driver(queued, (8, 12, 19))
        assert not replay.advance(at=tail_print)
        held = queued.snapshot(close)
        assert held.cash[0].available == 19 and not held.positions
        with pytest.raises(TiveloError) as error:
            replay.advance(at=tomorrow, require_complete=True)
        assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
        assert not queued.ledger and queued.snapshot(close) == held

        calls = 0
        with monkeypatch.context() as patch:
            patch.setattr(rules.execution_rules, "sellable_at", bad_settlement)
            with pytest.raises(RuntimeError, match="second allocation failed"):
                replay.advance(at=tomorrow)
        assert not queued.ledger and queued.snapshot(close) == held
        read_trades = reader.trade_prints

        def read_window(query):
            assert query.sequence is None
            return read_trades(query)

        with monkeypatch.context() as patch:
            patch.setattr(reader, "trade_prints", read_window)
            prefix = replay.advance(at=tomorrow)
        assert replay.advance(at=tomorrow) == prefix
        with pytest.raises(TiveloError) as error:
            replay.advance(at=tomorrow, require_complete=True)
        assert error.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
        assert [(r.trade.sequence, r.trade.quantity) for r in prefix] == [(8, 60), (12, 20)]
        assert prefix[-1].trade.revision_id == "corrected-before-delivery"
        assert [r.fills[0].filled_at for r in prefix] == [closing_print, next_print]
        assert all(r.fills[0].event_at == tomorrow for r in prefix)
        assert queued.snapshot(tomorrow).positions[0].sellable_quantity == 80
        (tail,) = replay.advance(at=tail_confirmed, require_complete=True)
        assert tail.trade.sequence == 19 and tail.fills[0].filled_quantity == 90
        assert tail.fills[0].event_at == tail_confirmed
        assert not replay.advance(at=exit_at, require_complete=True)
        assert [entry.quantity_delta for entry in queued.ledger] == [60, 20, 10]
        assert queued.snapshot(exit_at).positions[0].sellable_quantity == 90
        assert queued.snapshot(exit_at).cash[0].available == 4
        assert queued.confirm_expiry(ticket.order_id, at=exit_at)
        assert queued.snapshot(exit_at).cash[0].available == Decimal("30.1")
        queued.finish(exit_at)

        incomplete, submit, _ = context("235", expiry="confirmed_end_of_day")
        submit(start)
        wrong = driver(incomplete, (8, 19))
        with pytest.raises(TiveloError) as error:
            wrong.advance(at=tomorrow)
        assert error.value.code == ErrorCode.DATA_CONFLICT and not incomplete.ledger

        manual, submit, match = context("235", expiry="confirmed_end_of_day")
        submit(start)
        indexed = driver(manual, (8, 12, 19))
        witnessed = match(next_print, 12, confirmed_at=tail_print)
        assert witnessed.trade.quantity == 25
        with pytest.raises(TiveloError) as error:
            indexed.advance(at=tomorrow)
        assert error.value.code == ErrorCode.DATA_CONFLICT
        assert [entry.quantity_delta for entry in manual.ledger] == [25]

        return account.orders, account.ledger, partial, sold, report, overnight_report, prefix, tail

    with (
        engine.open_reader(snapshot.snapshot_id) as native,
        PortableSnapshotReader(bundle) as portable,
    ):
        assert play(native) == play(portable)
        trade_scope = next(
            v.contents.scope for v in native.datasets if v.version_id == trade_ref.version_id
        )
    memory = InMemoryReader(
        trade_prints=tape,
        knowledge_basis="historical_versions",
        scopes={"trade_print": trade_scope},
    )
    (memory_ref,) = memory.manifest.contents.datasets
    assert memory.trade_prints(audit.model_copy(update={"dataset": memory_ref})) == ()
    assert memory.trade_prints(
        audit.model_copy(update={"dataset": memory_ref, "as_of": first})
    ) == (original,)


STRATEGY_SOURCE = b"""
import json
from pathlib import Path
import polars as pl
from tivelo_backtest.research.factor_definitions import load_factor_definitions
from tivelo_backtest.research.signal_plan import compile_daily_signals
from tivelo_contracts.execution import Signal, SignalBatch
from .weights import read_weight

class Strategy:
    def __init__(self, weight, signals):
        self.weight = weight
        self.signals = signals

    def decide(self, context):
        assert all(bar.available_at <= context.now for bar in context.history)
        # Only raw price fields cross this explicit v2-to-research mapping;
        # no share volume is silently treated as legacy lots.
        panel = pl.DataFrame([
            {'symbol': bar.instrument_id, 'date': bar.trading_date,
             'open': float(bar.open), 'close': float(bar.close)}
            for bar in context.history
        ]).sort(['symbol', 'date'])
        values = self.signals.evaluate(panel)
        score = values['uf_price_ratio'][-1]
        assert score == 1.0
        side = {2: 'buy', 3: 'sell'}.get(context.now.day)
        if side is not None and not values['csg_' + side][-1]:
            side = None
        return SignalBatch(decision_at=context.now, signals=() if side is None else (
            Signal(instrument_id='A', decision_at=context.now, side=side, score=str(score),
                   source_id='strategy.fixture', target_weight=self.weight if side == 'buy' else '0'),
        ))

def build(*, parameters_json, dependencies):
    definitions = json.loads(Path(__file__).with_name('factors.json').read_text(encoding='utf-8'))
    signals = json.loads(Path(__file__).with_name('signals.json').read_text(encoding='utf-8'))
    plan = compile_daily_signals(signals, catalog=load_factor_definitions(definitions))
    return Strategy(read_weight(parameters_json), plan)
"""


def replay_portable_bundle(directory):
    """Used by the wheel probe in a separate environment without tivelo-data."""
    from tivelo_backtest.archives import read_result
    from tivelo_contracts.base import canonical_bytes

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "tivelo_backtest.cli",
            "run",
            "--run",
            str(directory / "run"),
            "--snapshot",
            str(directory / "portable"),
            "--output",
            str(directory / "replayed-result"),
        ],
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr
    _, result, metrics = read_result(directory / "replayed-result")
    assert canonical_bytes(result) == (directory / "expected-result.json").read_bytes()
    assert result.final_portfolio.cash[0].total == Decimal("5237.58")
    assert metrics.total_return == Decimal("0.047516")
    controlled = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "tivelo_backtest.cli",
            "run",
            "--run",
            str(directory / "controlled-run"),
            "--snapshot",
            str(directory / "portable"),
            "--output",
            str(directory / "controlled-replayed-result"),
        ],
        text=True,
        capture_output=True,
    )
    assert controlled.returncode == 0, controlled.stderr
    controlled_lock, controlled_result, controlled_metrics = read_result(
        directory / "controlled-replayed-result"
    )
    assert controlled_lock.contents.scale_proof.numeric_semantics == "anchor_cancelled_float64_v1"
    assert (
        canonical_bytes(controlled_result)
        == (directory / "expected-controlled-result.json").read_bytes()
    )
    assert controlled_metrics.final_equity == Decimal("5237.58")
    from tivelo_backtest.study_archives import read_study

    for mode in ("grid", "walk_forward"):
        original = directory / f"study-{mode}"
        replayed = directory / f"study-{mode}-replayed"
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "tivelo_backtest.cli",
                "study-replay",
                "--archive",
                str(original),
                "--snapshot",
                str(directory / "portable"),
                "--output",
                str(replayed),
            ],
            text=True,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert read_study(replayed) == read_study(original)


def signal(side, day, hour=7):
    return Signal(
        instrument_id="A", decision_at=at(day, hour), side=side, score="1", source_id="synthetic"
    )


def execute(account, market, day, price, hour=1):
    rules, security = market
    reference = ReferencePrice(
        instrument_id="A",
        trading_date=date(2025, 1, day),
        value="10",
        kind="previous_close",
        available_at=at(day, 0),
        source_id="synthetic",
        revision_id=f"p{day}",
    )
    account.match_open(
        at=at(day, hour),
        instruments={"A": security},
        raw_prices={"A": Decimal(price)},
        references={"A": reference},
        rules=rules,
    )


def test_cn_rules_drive_fills_t_plus_one_and_itemized_ledger(market):
    assert isinstance(market[0], ExecutionRules)
    account = CashEquityAccount("CNY", "5000")
    account.submit(signal("buy", 2), Decimal(200), eligible_at=at(3))
    execute(account, market, 3, "10")
    assert account.snapshot(at(3)).cash[0].total == Decimal("2994.98")
    assert account.snapshot(at(3)).positions[0].sellable_quantity == 0
    account.submit(signal("sell", 3, 2), Decimal(200), eligible_at=at(3, 3))
    execute(account, market, 3, "10", hour=3)
    assert account.orders[-1].reason == "insufficient_sellable_quantity"
    account.submit(signal("sell", 3), Decimal(200), eligible_at=at(6))
    execute(account, market, 6, "11")
    assert account.snapshot(at(6)).cash[0].total == Decimal("5188.86")
    assert account.snapshot(at(6)).positions == ()
    assert [(part.fee_id, part.amount) for part in account.orders[-1].fee_components] == [
        ("commission", Decimal(5)),
        ("stamp", Decimal("1.1")),
        ("transfer", Decimal("0.02")),
    ]
    assert sum(row.cash_delta for row in account.ledger) == Decimal("188.86")
    assert sum(row.quantity_delta for row in account.ledger) == 0


@pytest.mark.parametrize("venue", ["XSHG", "XSHE"])
def test_preset_spring_festival_calendar_drives_exact_account_settlement(market, venue):
    from tivelo_backtest.market_loading import discover_market_adapter
    from tivelo_market_cn.profile_presets import CashEquityProfileRequest, compile_market_adapter

    _, security = market
    adapter = compile_market_adapter(
        CashEquityProfileRequest(
            venue_id=venue,
            start=date(2025, 1, 24),
            end=date(2025, 2, 6),
            broker_policy=compiled_fee_policy(("handling", "regulatory")).inputs.policy,
            calendar_source="preset",
        )
    )
    loaded = discover_market_adapter(adapter).load()
    (classification,) = loaded.configuration.instrument_classes
    security = classification.bind(
        security.model_copy(update={
            "venue_id": venue,
            "calendar_id": loaded.calendar.version.contents.calendar_id,
        })
    )
    account = CashEquityAccount("CNY", "5000")

    def moment(day, clock="09:30"):
        return datetime.fromisoformat(f"{day}T{clock}:00+08:00").astimezone(UTC)

    def fill(day, price, clock="09:30"):
        timestamp = moment(day, clock)
        account.match_open(
            at=timestamp,
            instruments={"A": security},
            raw_prices={"A": Decimal(price)},
            rules=loaded,
            references={
                "A": ReferencePrice(
                    instrument_id="A",
                    trading_date=date.fromisoformat(day),
                    value="10",
                    kind="previous_close",
                    available_at=moment(day, "00:00"),
                    source_id="synthetic",
                    revision_id=day,
                )
            },
        )

    # Prices, classification and broker policy are synthetic.
    account.submit(
        Signal(
            instrument_id="A",
            decision_at=moment("2025-01-24", "15:00"),
            side="buy",
            score="1",
            source_id="synthetic",
        ),
        Decimal(200),
        eligible_at=moment("2025-01-27"),
    )
    fill("2025-01-27", "10")
    assert account.snapshot(moment("2025-01-27")).cash[0].total == Decimal("2994.98")
    assert account.snapshot(moment("2025-02-04", "15:00")).positions[0].sellable_quantity == 0
    assert loaded.sellable_at(security, moment("2025-01-27")) == moment("2025-02-05")
    account.submit(
        Signal(
            instrument_id="A",
            decision_at=moment("2025-01-27", "10:00"),
            side="sell",
            score="1",
            source_id="synthetic",
        ),
        Decimal(200),
        eligible_at=moment("2025-01-27", "13:00"),
    )
    fill("2025-01-27", "10", "13:00")
    assert account.orders[-1].reason == "insufficient_sellable_quantity"
    account.submit(
        Signal(
            instrument_id="A",
            decision_at=moment("2025-01-27", "15:00"),
            side="sell",
            score="1",
            source_id="synthetic",
        ),
        Decimal(200),
        eligible_at=moment("2025-02-05"),
    )
    fill("2025-02-05", "10.9")
    final = account.snapshot(moment("2025-02-05"))
    assert final.positions == ()
    # 5000 - 2000 - 5.02 + 2180 - (5 + 1.09 + 0.02).
    assert final.cash[0].total == Decimal("5168.87")
    assert sum(row.cash_delta for row in account.ledger) == Decimal("168.87")


def compiled_fee_policy(includes):
    from tivelo_market_cn.fee_presets import (
        BrokerFeePolicy,
        FeeRounding,
        compile_fee_rules,
        load_fee_preset,
    )

    policy = BrokerFeePolicy(
        policy_id="synthetic-broker-policy",
        policy_version="1",
        account_scope="domestic_cash_standard",
        aggregation="per_fill_research",
        commission_rate="0.0002",
        commission_minimum="5",
        commission_includes=includes,
        rounding=tuple(
            FeeRounding(fee_id=key, quantum="0.01", rounding="half_up")
            for key in sorted(
                {"commission", "handling", "regulatory", "stamp", "transfer"} - set(includes)
            )
        ),
        source_reference="synthetic-policy-not-an-official-commission-default",
        valid_from=at(1, 0),
        available_at=at(1, 0),
        revision_id="synthetic-1",
        source_id="synthetic",
    )
    return compile_fee_rules(
        load_fee_preset(version="2026.1.0"),
        policy,
        venue_id="XSHG",
        start=date(2025, 1, 2),
        end=date(2025, 1, 7),
    )


@pytest.mark.parametrize(
    "includes,buy_fee,sell_fee,final_cash",
    [
        ((), "5.13", "6.18", "5088.69"),
        (("handling", "regulatory"), "5.02", "6.07", "5088.91"),
        (("handling", "regulatory", "transfer"), "5.00", "6.05", "5088.95"),
    ],
)
def test_official_fee_rates_and_explicit_broker_policy_reach_exact_account(
    market, includes, buy_fee, sell_fee, final_cash
):
    from importlib.metadata import entry_points

    from tivelo_market_cn.adapter import CNMarketProfile

    original, security = market
    compiled = compiled_fee_policy(includes)
    profile = CNMarketProfile(
        profile_format="tivelo-cn-profile-v2",
        fee_compilation=compiled,
        configuration=original.configuration.model_copy(
            update={
                "schema_version": "1.2",
                "required_fee_ids": compiled.required_fee_ids,
                "fee_rules": compiled.fee_rules,
            }
        ),
        calendar=original.calendar.version,
    )
    (entry,) = entry_points(group="tivelo.market_rules.v1", name="tivelo.market.cn.cash-equity")
    rules = entry.load()(configuration_json=profile.model_dump_json())
    account = CashEquityAccount("CNY", "5000")
    account.submit(signal("buy", 2), Decimal(200), eligible_at=at(3))
    execute(account, (rules, security), 3, "10")
    assert account.orders[-1].status == "filled"
    account.submit(signal("sell", 3), Decimal(200), eligible_at=at(6))
    execute(account, (rules, security), 6, "10.5")
    filled = [item for item in account.orders if item.status == "filled"]
    assert [item.fee for item in filled] == [Decimal(buy_fee), Decimal(sell_fee)]
    assert account.snapshot(at(6)).cash[0].total == Decimal(final_cash)
    assert account.snapshot(at(6)).positions == ()
    for order in filled:
        assert not set(includes) & {item.fee_id for item in order.fee_components}
        assert all(
            item.source_reference.startswith("cn.fees.research-per-fill:")
            for item in order.fee_components
        )
    assert (
        sum((item.cash_delta for item in account.ledger), Decimal(0)) == Decimal(final_cash) - 5000
    )


def test_cn_rules_block_limit_open_but_preserve_original_gtc_decision(market):
    account = CashEquityAccount("CNY", "5000", order_expiry="good_till_cancelled")
    account.submit(signal("buy", 2), Decimal(200), eligible_at=at(3))
    execute(account, market, 3, "11")
    assert account.orders[-1].reason == "limit_up"
    execute(account, market, 6, "10")
    assert account.orders[-1].status == "filled"
    assert account.orders[-1].decision_at == at(2, 7)
    assert account.snapshot(at(6)).positions[0].sellable_quantity == 0
    assert account.snapshot(at(7)).positions[0].sellable_quantity == 200


def test_cn_minimum_quantity_does_not_silently_resize_order(market):
    account = CashEquityAccount("CNY", "5000")
    account.submit(signal("buy", 2), Decimal(199), eligible_at=at(3))
    execute(account, market, 3, "10")
    assert account.orders[-1].reason == "invalid_quantity"
    assert account.ledger == ()
    assert account.snapshot(at(3)).cash[0].total == 5000


@pytest.mark.parametrize("order_type", ["market", "limit"])
def test_installed_typed_cn_profile_drives_exact_account_admission(market, order_type):
    from tivelo_backtest.price_views import StaticPriceView
    from tivelo_contracts.data import Bar
    from tivelo_contracts.execution import PriceViewSpec

    market = configured_order_types(market)
    account = CashEquityAccount("CNY", "2000000")
    instruction = signal("buy", 2)
    view = None
    if order_type == "limit":
        view = StaticPriceView("raw")
        view.project(
            (
                Bar(
                    instrument_id="A",
                    timeframe="1d",
                    bar_start=at(2),
                    bar_end=at(2, 7),
                    trading_date=date(2025, 1, 2),
                    session_id="fixture.daily",
                    open="10",
                    high="10",
                    low="10",
                    close="10",
                    volume="1000",
                    turnover="10000",
                    quantity_unit="share",
                    quote_currency="CNY",
                    price_basis="raw",
                    available_at=at(2, 7),
                    ingested_at=at(2, 7),
                    source_id="synthetic",
                    revision_id="1",
                ),
            ),
            (),
            PriceViewSpec(provider_id=view.provider_id, mode="raw"),
            decision_at=at(2, 7),
        )
        instruction = instruction.model_copy(
            update={"limit_price": view.price("10", instrument_id="A", decision_at=at(2, 7))}
        )
    account.submit(instruction, Decimal(60000), eligible_at=at(3), price_view=view)
    execute(account, market, 3, "10")
    if order_type == "limit":
        assert account.orders[-1].status == "filled"
        assert account.orders[-1].fee == 126
        assert account.orders[-1].raw_limit_price == 10
        assert account.snapshot(at(3)).cash[0].total == 1399874
    else:
        assert account.orders[-1].reason == "invalid_quantity"
        assert account.ledger == ()
        assert account.snapshot(at(3)).cash[0].total == 2000000


@pytest.mark.parametrize("tighten_quantity", [False, True])
def test_pending_order_uses_historical_classification_and_current_rules(market, tighten_quantity):
    from tivelo_market_cn.adapter import CNMarketProfile, create_rules

    rules, securities = configured_rule_history(market)
    if tighten_quantity:
        *previous, latest = rules.configuration.terms
        quantities = tuple(
            item.model_copy(update={"buy_maximum": Decimal(200)})
            if item.order_type == "market"
            else item
            for item in latest.order_quantity_rules
        )
        configuration = rules.configuration.model_copy(
            update={
                "terms": (*previous, latest.model_copy(update={"order_quantity_rules": quantities}))
            }
        )
        rules = create_rules(
            configuration_json=CNMarketProfile(
                configuration=configuration, calendar=rules.calendar.version
            ).model_dump_json()
        )
    account = CashEquityAccount("CNY", "200000", order_expiry="good_till_cancelled")
    account.submit(signal("buy", 2), Decimal(10000), eligible_at=at(3))
    historical_market = (rules, securities[-1])
    execute(account, historical_market, 3, "10.5")
    assert account.orders[-1].reason == "limit_up"
    assert account.ledger == ()
    execute(account, historical_market, 6, "10.5")
    assert account.orders[-1].decision_at == at(2, 7)
    if tighten_quantity:
        assert account.orders[-1].reason == "invalid_quantity"
        assert account.ledger == ()
    else:
        assert account.orders[-1].status == "filled"
        commission = next(
            part for part in account.orders[-1].fee_components if part.fee_id == "commission"
        )
        assert commission.amount == Decimal("31.50")
        assert commission.rule_revision == "commission-next"
        assert account.snapshot(at(6)).cash[0].total == Decimal("94967.45")


@pytest.mark.parametrize(
    "quantity,cash,remaining", [(99, "4320.48", 200), (199, "4654.31", 100), (198, "3994.99", 299)]
)
def test_real_cn_remainder_policy_drives_post_action_partial_sales(
    market, quantity, cash, remaining
):
    from tivelo_contracts.data import CorporateAction
    from tivelo_market_cn.adapter import CNMarketProfile, create_rules
    from tivelo_market_cn.classification import CNEquityClass
    from tivelo_market_cn.quantity_presets import load_quantity_preset, resolve_quantity_rules

    original, security = market
    state = CNEquityClass(
        venue_id="XSHG", board="main", risk_status="normal", listing_phase="regular"
    )
    selected = resolve_quantity_rules(load_quantity_preset(version="2026.1.0"), state, at=at(3))
    security = state.bind(security)
    (term,) = original.configuration.terms
    configuration = original.configuration.model_copy(
        update={
            "schema_version": "1.3",
            "instrument_classes": (state,),
            "terms": (
                term.model_copy(
                    update={
                        "buy_minimum": Decimal(100),
                        "buy_increment": Decimal(100),
                        "sell_minimum": Decimal(100),
                        "sell_increment": Decimal(100),
                        "odd_lot_policy": "remainder_once",
                        "spec_revision": state.spec_revision,
                        "order_quantity_rules": selected.order_quantities,
                        "source_reference": selected.rule_id,
                    }
                ),
            ),
        }
    )
    rules = create_rules(
        configuration_json=CNMarketProfile(
            configuration=configuration,
            calendar=original.calendar.version,
        ).model_dump_json()
    )
    account = CashEquityAccount("CNY", "5000")
    account.submit(signal("buy", 2), Decimal(100), eligible_at=at(3))
    execute(account, (rules, security), 3, "10")
    event = CorporateAction(
        action_id="synthetic-split",
        instrument_id="A",
        action_type="split",
        quantity_ratio="2.99",
        effective_at=at(6, 0),
        available_at=at(2, 0),
        source_id="synthetic",
        revision_id="1",
    )
    account.apply_action(event, at=at(6, 0))
    account.submit(signal("sell", 6, 0), Decimal(quantity), eligible_at=at(6))
    account.match_open(
        at=at(6),
        instruments={"A": security},
        raw_prices={"A": Decimal("3.34")},
        rules=rules,
        references={
            "A": ReferencePrice(
                instrument_id="A",
                trading_date=date(2025, 1, 6),
                value="3.34",
                kind="exchange_ex_rights",
                available_at=at(6, 0),
                source_id="synthetic",
                revision_id="ex-rights",
            )
        },
    )
    assert account.orders[-1].reason == ("invalid_quantity" if quantity == 198 else None)
    portfolio = account.snapshot(at(6))
    assert portfolio.cash[0].total == Decimal(cash)
    assert portfolio.positions[0].quantity == portfolio.positions[0].sellable_quantity == remaining
    assert sum(row.quantity_delta for row in account.ledger) == remaining


def test_published_data_snapshot_drives_the_independent_daily_runner(market, tmp_path):
    from tivelo_backtest.daily import VERIFIER_VERSION, DailyBacktest
    from tivelo_backtest.definitions import DefinitionLoader, seal_definition
    from tivelo_backtest.price_views import StaticPriceView
    from tivelo_backtest.runtime import capture_runtime, verify_runtime
    from tivelo_contracts.base import canonical_bytes
    from tivelo_contracts.data import (
        AdjustmentFactor,
        Bar,
        Coverage,
        CoverageIssue,
        HistoryQuery,
        OpeningObservation,
    )
    from tivelo_contracts.execution import (
        BacktestSpec,
        ExecutionModel,
        PriceViewSpec,
        RunLock,
        RunLockContents,
    )
    from tivelo_data.engine import DataEngine
    from tivelo_data.providers import (
        FetchPage,
        FixedSourceResolver,
        ProviderCapability,
        ProviderDescriptor,
        SyncRequest,
    )
    from tivelo_market_cn.adapter import CNMarketProfile, create_rules
    from tivelo_market_cn.price_presets import compile_price_terms, load_price_preset

    rules, securities = configured_rule_history(market)
    compiled = compiled_fee_policy(("handling", "regulatory"))
    # Synthetic issuer history: warning status is removed on Jan 6, announced
    # Jan 5. Official 2025 warning prices remain 5%; the old synthetic 10%
    # warning rule must not be relabelled as an official historical rule.
    securities = (
        *securities,
        securities[-1].model_copy(
            update={"valid_to": at(6, 0), "available_at": at(5, 0), "revision_id": "warning-ended"}
        ),
        securities[0].model_copy(
            update={
                "valid_from": at(6, 0),
                "available_at": at(5, 0),
                "revision_id": "normal-restored",
            }
        ),
    )
    price_preset = load_price_preset(version="2026.1.0")
    prices = tuple(
        compile_price_terms(
            price_preset,
            classification,
            tuple(
                term.model_copy(update={"valid_to": at(7, 0)})
                for term in rules.configuration.terms
                if term.spec_revision == classification.spec_revision
            ),
        )
        for classification in rules.configuration.instrument_classes
    )
    market_profile = CNMarketProfile(
        profile_format="tivelo-cn-profile-v3",
        fee_compilation=compiled,
        price_compilations=prices,
        configuration=rules.configuration.model_copy(
            update={
                "terms": tuple(term for compilation in prices for term in compilation.terms),
                "required_fee_ids": compiled.required_fee_ids,
                "fee_rules": compiled.fee_rules,
            }
        ),
        calendar=rules.calendar.version,
    )
    rules = create_rules(configuration_json=market_profile.model_dump_json())
    bars = tuple(
        Bar(
            instrument_id="A",
            timeframe="1d",
            bar_start=at(day),
            bar_end=at(day, 7),
            trading_date=date(2025, 1, day),
            session_id="fixture.daily",
            open=price,
            high=price,
            low=price,
            close=price,
            volume="10000" if day == 2 else "11000",
            turnover="100000",
            quantity_unit="share",
            quote_currency="CNY",
            price_basis="raw",
            available_at=at(day, 7),
            ingested_at=at(day, 7),
            source_id="synthetic",
            revision_id="1",
        )
        for day, price in ((2, "10"), (3, "10"), (6, "11"))
    )
    openings = tuple(
        OpeningObservation(
            instrument_id="A",
            event_at=bar.bar_start,
            trading_date=bar.trading_date,
            session_id="fixture.open",
            state="tradable",
            raw_open=bar.open,
            quote_currency="CNY",
            reference_price="10",
            reference_kind="previous_close",
            available_at=bar.bar_start,
            ingested_at=bar.bar_start,
            source_id="synthetic",
            revision_id="1",
        )
        for bar in bars
    )

    class FixtureProvider:
        def __init__(self, kind, records):
            self.kind, self.records = kind, records

        def describe(self):
            return ProviderDescriptor(
                provider_id="fixture",
                version="1",
                source_ids=("synthetic",),
                capabilities=(
                    ProviderCapability(
                        record_type=self.kind,
                        market_id="cn.equity",
                        asset_class="equity",
                        timeframes=("1d",) if self.kind == "bar" else (),
                        price_bases=("raw",) if self.kind == "bar" else (),
                        knowledge_basis="historical_versions",
                        normalization_version="synthetic-v1",
                        bar_convention_id="fixture.daily" if self.kind == "bar" else None,
                    ),
                ),
            )

        def check_ready(self, context):
            context.check_cancelled()

        def fetch(self, request, cursor, context):
            assert cursor is None
            return FetchPage(
                self.records,
                None,
                Coverage(
                    status="complete" if self.records else "empty",
                    requested_units=1,
                    completed_units=1,
                    row_count=len(self.records),
                    issues=()
                    if self.records
                    else (
                        CoverageIssue(
                            instrument_id="A",
                            reason="no_events",
                            detail="Synthetic source confirms no company events in this range",
                        ),
                    ),
                ),
            )

        def close(self):
            pass

    engine = DataEngine(tmp_path / "published")
    versions = []
    for kind, records in (
        ("bar", bars),
        ("instrument", securities),
        ("opening_observation", openings),
        ("corporate_action", ()),
        (
            "adjustment_factor",
            (
                AdjustmentFactor(
                    factor_id="baseline",
                    instrument_id="A",
                    value="1",
                    effective_at=at(1, 0),
                    available_at=at(1, 0),
                    factor_kind="cumulative_forward",
                    definition_id="fixture.cumulative",
                    baseline_id="fixture.listing",
                    source_id="synthetic",
                    revision_id="1",
                ),
            ),
        ),
    ):
        engine.resolver = FixedSourceResolver(FixtureProvider(kind, records), "a" * 64)
        request = SyncRequest(
            dataset_id=kind,
            record_type=kind,
            market_id="cn.equity",
            asset_class="equity",
            instrument_ids=("A",),
            start=at(1, 0),
            end=at(7, 0),
            timeframe="1d" if kind == "bar" else None,
            price_basis="raw" if kind == "bar" else None,
        )
        versions.append(engine.publish(engine.sync(engine.plan_sync(request))).version_id)
    snapshot = engine.create_snapshot(tuple(versions))
    with engine.open_reader(snapshot.snapshot_id) as reader:
        for moment, revision, ratio in (
            (at(2), "1", "0.1"),
            (at(2, 13), "normal-ended", "0.1"),
            (at(3), "warning", "0.05"),
            (at(6), "normal-restored", "0.1"),
        ):
            (selected,) = reader.instruments(
                HistoryQuery(instrument_ids=("A",), effective_at=moment, as_of=moment)
            )
            assert selected.revision_id == revision
            assert rules.trading_terms(selected, at=moment).limit_up_ratio == Decimal(ratio)
            if revision == "1":
                assert selected.valid_to is None
    source = tmp_path / "strategy-source"
    source.mkdir()
    (source / "strategy.py").write_bytes(STRATEGY_SOURCE)
    (source / "weights.py").write_bytes(
        b"import json\ndef read_weight(parameters): return json.loads(parameters)['weight']\n"
    )
    (source / "factors.json").write_text(
        '[{"id":"uf_price_ratio","label":"Price ratio","formula":"close / open"}]\n',
        encoding="utf-8",
    )
    (source / "signals.json").write_text(
        json.dumps(
            [
                {
                    "id": "buy",
                    "name": "Buy",
                    "kind": "entry",
                    "conditions": [
                        {"left": "uf_price_ratio", "op": ">=", "right": 1},
                    ],
                },
                {
                    "id": "sell",
                    "name": "Sell",
                    "kind": "exit",
                    "conditions": [
                        {"left": "close", "op": ">=", "right": "field:close", "rightDays": 1},
                    ],
                },
            ]
        ),
        encoding="utf-8",
    )
    bundle = seal_definition(
        source,
        definition_id="strategy.fixture",
        version="1",
        kind="strategy",
        entrypoint="strategy:build",
        files=("strategy.py", "weights.py", "factors.json", "signals.json"),
    )
    (source / "definition.json").write_bytes(canonical_bytes(bundle))
    loader = DefinitionLoader()
    definitions = loader.inspect(source, parameters_json='{"weight":"0.5"}')
    reference = definitions.root
    runtime = capture_runtime(("tivelo-backtest", "tivelo-market-cn"))
    spec = BacktestSpec(
        semantics_version="strict.daily.v1",
        snapshot_id=snapshot.snapshot_id,
        universe=("A",),
        start="2025-01-02",
        end="2025-01-06",
        strategy=reference,
        market_profile=rules.reference,
        account_profile=CashEquityAccount("CNY", "5000").reference,
        price_view=PriceViewSpec(provider_id="tivelo.raw", mode="raw"),
        execution=ExecutionModel(timeframe="1d"),
        accounting_model="event_ledger",
        knowledge_requirement="historical_versions",
        initial_capital="5000",
        base_currency="CNY",
    )
    lock = RunLock.create(
        RunLockContents(
            spec=spec,
            snapshot=snapshot,
            packages=runtime.packages,
            runtime=runtime.reference,
            verifier_version=VERIFIER_VERSION,
        )
    )

    with (
        engine.open_reader(snapshot.snapshot_id) as reader,
        loader.load_trusted_python(definitions) as binding,
    ):
        result = DailyBacktest(rules=rules, price_view=StaticPriceView("raw")).run(
            lock, reader, binding
        )
    assert [
        (event.filled_at.day, event.filled_quantity, event.fee)
        for event in result.orders
        if event.status == "filled"
    ] == [(3, Decimal(249), Decimal("5.02")), (6, Decimal(249), Decimal("6.4"))]
    assert result.final_portfolio.cash[0].total == Decimal("5237.58")
    assert result.final_portfolio.positions == ()
    assert result.lock_id == lock.lock_id
    assert sum(entry.cash_delta for entry in result.ledger) == Decimal("237.58")
    # Close/reopen the real immutable snapshot and obtain byte-identical results.
    with (
        engine.open_reader(snapshot.snapshot_id) as reader,
        loader.load_trusted_python(definitions) as binding,
    ):
        replay = DailyBacktest(rules=rules, price_view=StaticPriceView("raw")).run(
            lock, reader, binding
        )
    assert replay.model_dump_json() == result.model_dump_json()
    # A portable bundle keeps the original snapshot identity and needs no data index.
    from tivelo_backtest.snapshot import PortableSnapshotReader

    exported = tmp_path / "portable"
    engine.export_snapshot(snapshot.snapshot_id, exported)
    engine.store.delete_snapshot(snapshot.snapshot_id)
    with (
        PortableSnapshotReader(exported, snapshot_id=snapshot.snapshot_id) as reader,
        loader.load_trusted_python(definitions) as binding,
    ):
        portable = DailyBacktest(rules=rules, price_view=StaticPriceView("raw")).run(
            lock, reader, binding
        )
    assert portable.model_dump_json() == result.model_dump_json()
    verify_runtime(runtime, ("tivelo-backtest", "tivelo-market-cn"))
    # The real CLI resolves installed rules and persists the definition closure and results.
    from tivelo_backtest.archives import read_result
    from tivelo_backtest.runner import RunRequest
    from tivelo_contracts.execution import MarketAdapterSpec

    request = RunRequest(
        universe=("A",),
        start="2025-01-02",
        end="2025-01-06",
        strategy_parameters_json='{"weight":"0.5"}',
        market=MarketAdapterSpec(
            adapter_id="tivelo.market.cn.cash-equity",
            distribution="tivelo-market-cn",
            configuration_json=market_profile.model_dump_json(),
        ),
        account=CashEquityAccount("CNY", "5000").policy,
        initial_capital="5000",
        price_view=PriceViewSpec(provider_id="tivelo.raw", mode="raw"),
    )
    request_path = tmp_path / "request.json"
    request_path.write_bytes(canonical_bytes(request))

    def cli(*arguments):
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "tivelo_backtest.cli", *map(str, arguments)],
            text=True,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    cli(
        "prepare",
        "--request",
        request_path,
        "--definition",
        source,
        "--snapshot",
        exported,
        "--output",
        tmp_path / "run",
    )
    from tivelo_market_cn.order_prices import continuous_limit_reference
    from tivelo_market_cn.presets import compile_calendar, load_calendar_preset

    cage_profile = market_profile.model_copy(update={
        "profile_format": "tivelo-cn-profile-v4",
        "continuous_limit": continuous_limit_reference(version="2026.1.0"),
        "calendar": compile_calendar(
            load_calendar_preset(version="2026.1.0"), venue_id="XSHG",
            start=request.start, end=request.end,
        ),
    })
    cage_request = request.model_copy(update={
        "market": request.market.model_copy(update={"configuration_json": cage_profile.model_dump_json()}),
    })
    cage_path = tmp_path / "continuous-limit-request.json"
    cage_path.write_bytes(canonical_bytes(cage_request))
    absent = tmp_path / "daily-run-with-order-book-required"
    denied = subprocess.run(
        [sys.executable, "-I", "-m", "tivelo_backtest.cli", "prepare",
         "--request", str(cage_path), "--definition", str(source),
         "--snapshot", str(exported), "--output", str(absent)],
        text=True, capture_output=True,
    )
    assert denied.returncode == 1 and not absent.exists()
    assert json.loads(denied.stderr)["code"] == "UNSUPPORTED_CAPABILITY"

    # Later source edits cannot change the already prepared job.
    (source / "strategy.py").write_text(
        "raise RuntimeError('original was edited')\n", encoding="utf-8"
    )
    (source / "factors.json").write_text("[]\n", encoding="utf-8")
    (source / "signals.json").write_text("[]\n", encoding="utf-8")
    arguments = (
        "run",
        "--run",
        tmp_path / "run",
        "--snapshot",
        exported,
        "--output",
        tmp_path / "result",
    )
    first_publication = cli(*arguments)
    assert cli(*arguments) == first_publication
    archived_lock, archived_result, metrics = read_result(tmp_path / "result")
    assert archived_result.model_copy(update={"lock_id": result.lock_id}) == result
    assert metrics.final_equity == Decimal("5237.58")
    inspected = cli("inspect", "--archive", tmp_path / "result")
    assert inspected["metrics"]["total_return"] == "0.047516"
    assert archived_lock.contents.market_adapter == request.market
    # Test-only comparison bytes for the second isolated consumer; published archives above are real.
    (tmp_path / "run-lock.json").write_bytes(canonical_bytes(archived_lock))
    (tmp_path / "expected-result.json").write_bytes(canonical_bytes(archived_result))
    # The bounded strategy uses future-qfq anchor cancellation; fills/fees stay raw.
    controlled_source = tmp_path / "controlled-source"
    controlled_source.mkdir()
    from tivelo_backtest.controlled import ControlledProgram

    controlled_program = ControlledProgram(
        entry="close >= open",
        exit="volume > 10000",
        score="close / open",
        weight_per_position="0.5",
    )
    (controlled_source / "program.json").write_bytes(canonical_bytes(controlled_program))
    cli(
        "seal",
        "--root",
        controlled_source,
        "--id",
        "controlled.fixture",
        "--version",
        "1",
        "--language",
        "controlled_daily_v1",
        "--entrypoint",
        "program.json",
        "--file",
        "program.json",
    )
    controlled_request = request.model_copy(
        update={
            "strategy_parameters_json": "{}",
            "price_view": PriceViewSpec(
                provider_id="tivelo.qfq",
                mode="qfq",
                anchor_at=at(7, 0),
                factor_definition_id="fixture.cumulative",
                factor_baseline_id="fixture.listing",
            ),
        }
    )
    controlled_request_path = tmp_path / "controlled-request.json"
    controlled_request_path.write_bytes(canonical_bytes(controlled_request))
    cli(
        "prepare",
        "--request",
        controlled_request_path,
        "--definition",
        controlled_source,
        "--snapshot",
        exported,
        "--output",
        tmp_path / "controlled-run",
    )
    (controlled_source / "program.json").write_text("{}", encoding="utf-8")
    controlled_arguments = (
        "run",
        "--run",
        tmp_path / "controlled-run",
        "--snapshot",
        exported,
        "--output",
        tmp_path / "controlled-result",
    )
    controlled_publication = cli(*controlled_arguments)
    assert cli(*controlled_arguments) == controlled_publication
    controlled_lock, controlled_result, controlled_metrics = read_result(
        tmp_path / "controlled-result"
    )
    assert controlled_lock.contents.scale_proof.strategy == controlled_lock.contents.spec.strategy
    assert controlled_metrics == metrics
    assert controlled_result.orders == archived_result.orders
    assert controlled_result.ledger == archived_result.ledger
    inspected_controlled = cli("inspect", "--archive", tmp_path / "controlled-result")
    assert inspected_controlled["scale_proof"]["numeric_semantics"] == "anchor_cancelled_float64_v1"
    (tmp_path / "expected-controlled-result.json").write_bytes(canonical_bytes(controlled_result))
    from tivelo_backtest.study_archives import read_study
    from tivelo_backtest.study_models import (
        SearchAxis,
        SearchSpace,
        StudyRequest,
        WalkForwardWindows,
    )

    assert cli("study-schema") == StudyRequest.model_json_schema()
    for mode in ("grid", "walk_forward"):
        study_request = StudyRequest(
            mode=mode,
            search=SearchSpace(
                target="controlled_program",
                axes=(SearchAxis(name="weight_per_position", values=("0.5", "0.75")),),
            ),
            walk_forward=WalkForwardWindows(train_sessions=2, test_sessions=1, step_sessions=1)
            if mode == "walk_forward"
            else None,
        )
        path = tmp_path / f"study-{mode}-request.json"
        path.write_bytes(canonical_bytes(study_request))
        study_output = tmp_path / f"study-{mode}"
        arguments = (
            "study",
            "--request",
            path,
            "--run",
            tmp_path / "controlled-run",
            "--snapshot",
            exported,
            "--output",
            study_output,
        )
        study_publication = cli(*arguments)
        assert cli(*arguments) == study_publication
        manifest, plan, report = read_study(study_output)
        assert report.prepared_fact_bytes > 0
        outcome = report.windows[0]
        selected = next(
            candidate
            for candidate in plan.candidates
            if candidate.candidate_id == outcome.winner_candidate_id
        )
        if mode == "grid":
            assert json.loads(selected.overrides_json)["weight_per_position"] == "0.75"
            winning_trial = next(
                trial for trial in outcome.training if trial.candidate_id == selected.candidate_id
            )
            assert winning_trial.metrics.final_equity == Decimal("5361.86")
            half_weight = next(
                candidate
                for candidate in plan.candidates
                if json.loads(candidate.overrides_json)["weight_per_position"] == "0.5"
            )
            reference_trial = next(
                trial
                for trial in outcome.training
                if trial.candidate_id == half_weight.candidate_id
            )
            _, reference_result, reference_metrics = read_result(
                study_output / reference_trial.path
            )
            assert reference_metrics == controlled_metrics
            assert reference_result.orders == controlled_result.orders
            assert reference_result.ledger == controlled_result.ledger
        else:
            # IS ends on Jan 3: Jan 6's gain must not influence parameter selection.
            assert json.loads(selected.overrides_json)["weight_per_position"] == "0.5"
            assert plan.windows[0].train_end == date(2025, 1, 3)
            assert plan.windows[0].test_start == date(2025, 1, 6)
            assert report.oos_compounded_return == 0
            assert outcome.test.metrics.filled_orders == 0
        inspected_study = cli("inspect", "--kind", "study", "--archive", study_output)
        assert inspected_study["archive_id"] == manifest.archive_id
        assert inspected_study["report"] == report.model_dump(mode="json")
        path.unlink()  # Replay must recover the request from the study archive itself.
