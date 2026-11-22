"""Submit simulated cash-equity limit orders against a pinned continuous tape."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Context, Decimal, localcontext
from itertools import pairwise
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from tivelo_contracts.base import Contract, utc_datetime
from tivelo_contracts.data import DatasetReference, DatasetVersion, Instrument
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import DefinitionReference, OrderEvent, Signal
from tivelo_contracts.facts import BUSINESS_KEYS
from tivelo_contracts.market import FeeQuote, PriceBounds, ReferencePrice
from tivelo_contracts.protocols import DailyExecutionRules, PriceViewProvider

from .account import _POSITIVE, CashEquityAccount, ContinuousSubmission, _Order
from .order_rules import bind_order_rules

if TYPE_CHECKING:
    from tivelo_contracts.protocols import OrderBookReader
    from tivelo_contracts.trade_prints import TradePrint, TradePrintQuery


class _PriceDecision(Protocol):
    instrument_id: str
    side: str
    at: datetime
    raw_limit: Decimal
    result: str


class _SnapshotPriceDecision(Protocol):
    snapshot_id: str
    dataset: DatasetReference
    check: _PriceDecision


@runtime_checkable
class ContinuousLimitMarket(Protocol):
    @property
    def reference(self) -> DefinitionReference: ...

    @property
    def execution_rules(self) -> DailyExecutionRules: ...

    def check_snapshot_limit_price(
        self,
        instrument: Instrument,
        side: str,
        raw_limit: Decimal,
        *,
        at: datetime,
        reader: OrderBookReader,
        dataset: DatasetReference,
        reference: ReferencePrice | None,
    ) -> _SnapshotPriceDecision: ...

    def cash_limit_deadline(
        self, instrument: Instrument, side: str, *, at: datetime
    ) -> datetime: ...


@runtime_checkable
class ContinuousTradeMarket(ContinuousLimitMarket, Protocol):
    def continuous_session_id(self, at: datetime) -> str: ...


@dataclass(frozen=True)
class ContinuousTradeMatch:
    snapshot_id: str
    dataset: DatasetReference
    trade: TradePrint
    fills: tuple[OrderEvent, ...]
    confirmed_at: datetime | None = None


class ContinuousLimitAdmission:
    """Admit cash orders and replay strictly-through witnessed trades."""

    def __init__(
        self,
        account: CashEquityAccount,
        market: ContinuousLimitMarket,
        *,
        reader: OrderBookReader,
        dataset: DatasetReference,
        trade_dataset: DatasetReference | None = None,
    ):
        if not account._continuous_orders or not isinstance(market, ContinuousLimitMarket):
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Unsupported continuous account or market"
            )
        rules = market.execution_rules
        if not isinstance(rules, DailyExecutionRules) or rules.buy_cost_monotone is not True:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Market has no reservable cash fees"
            )
        if dataset not in reader.manifest.contents.datasets:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Order book is outside the pinned snapshot")
        if trade_dataset is not None and trade_dataset not in reader.manifest.contents.datasets:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Trade dataset is outside the pinned snapshot"
            )
        pin = reader.manifest.snapshot_id, dataset, market.reference, trade_dataset
        if account._continuous_pin not in (None, pin):
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Account already uses another market or snapshot"
            )
        account._continuous_pin = pin
        self._account, self._market, self._rules = account, market, rules
        self._reader, self._pin = reader, pin

    def _check_pin(self) -> None:
        if (
            self._reader.manifest.snapshot_id != self._pin[0]
            or self._market.reference != self._pin[2]
        ):
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Continuous replay inputs changed")

    def submit(
        self,
        signal: Signal,
        quantity: Decimal,
        *,
        at: datetime,
        instrument: Instrument,
        reference: ReferencePrice | None,
        price_view: PriceViewProvider,
    ) -> ContinuousSubmission:
        account = self._account
        at = utc_datetime(at)
        quantity = _POSITIVE.validate_python(quantity)
        requested = getattr(signal, "limit_quantity", None)
        if requested is not None and requested != quantity:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Accepted quantity differs from the signal")
        if signal.decision_at > at:
            raise TiveloError(ErrorCode.FUTURE_INFORMATION, "Order precedes its strategy decision")
        if signal.limit_price is None:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Continuous admission requires a limit"
            )
        if (
            instrument.instrument_id != signal.instrument_id
            or instrument.quote_currency != account.currency
            or instrument.settlement_currency != account.currency
        ):
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Order instrument does not match the cash account"
            )
        self._check_pin()
        snapshot_id, dataset, _, _ = self._pin
        inputs = instrument, reference
        previous = account._submitted.get((signal.instrument_id, signal.decision_at))
        if previous is not None:
            if (
                previous.signal,
                previous.quantity,
                previous.submitted_at,
                previous.admission_inputs,
            ) != (
                signal,
                quantity,
                at,
                inputs,
            ):
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT,
                    "A continuous order was resubmitted with different inputs",
                )
            assert previous.admission is not None
            return previous.admission
        account._time(at)
        raw_limit = account._raw_limit(signal, price_view)
        assert raw_limit is not None
        quote = self._market.check_snapshot_limit_price(
            instrument,
            signal.side,
            raw_limit,
            at=at,
            reader=self._reader,
            dataset=dataset,
            reference=reference,
        )
        check = quote.check
        if not isinstance(quote, Contract) or (
            quote.snapshot_id,
            quote.dataset,
            check.instrument_id,
            check.side,
            check.at,
            check.raw_limit,
        ) != (snapshot_id, dataset, signal.instrument_id, signal.side, at, raw_limit):
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Price decision does not match the submitted order"
            )

        expires_at, reserved = None, Decimal(0)
        reason = check.result
        if reason == "price_valid":
            expires_at = utc_datetime(
                self._market.cash_limit_deadline(instrument, signal.side, at=at)
            )
            if expires_at <= at:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Order deadline is at or before submission"
                )
            reason, reserved = self._review_resources(signal, quantity, instrument, raw_limit, at)
        elif reason not in ("invalid_limit_tick", "outside_daily_limits", "outside_price_cage"):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Unknown limit-price decision")
        order = _Order(
            order_id=f"order:{len(account._submitted) + 1}",
            signal=signal,
            quantity=quantity,
            submitted_at=at,
            eligible_at=at,
            raw_limit=raw_limit,
            expires_at=expires_at,
            reserved_cash=reserved,
            admission_inputs=inputs,
        )
        outcome = account._record_submission(order, reason=reason, price_check=quote)
        assert outcome.admission is not None
        return outcome.admission

    def match_trade(
        self,
        *,
        at: datetime,
        sequence: int,
        instrument: Instrument,
        reference: ReferencePrice | None,
        confirmed_at: datetime | None = None,
    ) -> ContinuousTradeMatch:
        """Consume one print at its confirmation while using its execution-time rules."""
        at = utc_datetime(at)
        confirmed_at = at if confirmed_at is None else utc_datetime(confirmed_at)
        version = self._trade_version(instrument, start=at)
        with self._trade_transaction(instrument.instrument_id):
            return self._match_trade(
                version,
                at=at,
                sequence=sequence,
                instrument=instrument,
                reference=reference,
                confirmed_at=confirmed_at,
            )

    def _trade_version(
        self, instrument: Instrument, *, start: datetime, end: datetime | None = None
    ) -> DatasetVersion:
        if "trade_print" not in BUSINESS_KEYS:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Trade-print contracts are unavailable"
            )
        from tivelo_contracts.protocols import TradePrintReader
        from tivelo_contracts.trade_prints import select_trade_dataset

        account = self._account
        self._check_pin()
        dataset = self._pin[3]
        if (
            dataset is None
            or not isinstance(self._reader, TradePrintReader)
            or not isinstance(self._market, ContinuousTradeMarket)
        ):
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Replay requires a pinned trade feed and market session",
            )
        version = select_trade_dataset(self._reader.datasets, dataset)
        if version.contents.knowledge_basis != "historical_versions":
            raise TiveloError(
                ErrorCode.FUTURE_INFORMATION, "Trade replay requires historical print versions"
            )
        scope = version.contents.scope
        key = instrument.instrument_id
        if scope is None or (scope.market_id, scope.asset_class) != (
            instrument.market_id,
            instrument.asset_class,
        ):
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Trade dataset has the wrong scope")
        if (
            key not in scope.instrument_ids
            or not scope.start <= start < scope.end
            or (end is not None and end > scope.end)
        ):
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Trade is outside the capture scope"
            )
        if (
            instrument.quote_currency != account.currency
            or instrument.settlement_currency != account.currency
        ):
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Trade instrument does not match the cash currency"
            )
        return version

    @contextmanager
    def _trade_transaction(self, key: str):
        account = self._account
        event_start, ledger_start = len(account._events), len(account._ledger)
        saved = account._cash, account._clock, account._funding_changed_at, account._pending.copy()
        lots = account._lots.get(key)
        previous_match = account._last_trade_matches.get(key)
        try:
            yield
        except BaseException:
            account._cash, account._clock, account._funding_changed_at, account._pending = saved
            if lots is None:
                account._lots.pop(key, None)
            else:
                account._lots[key] = lots
            if previous_match is None:
                account._last_trade_matches.pop(key, None)
            else:
                account._last_trade_matches[key] = previous_match
            del account._events[event_start:]
            del account._ledger[ledger_start:]
            raise

    def _match_trade(
        self,
        version: DatasetVersion,
        *,
        at: datetime,
        sequence: int,
        instrument: Instrument,
        reference: ReferencePrice | None,
        confirmed_at: datetime,
        record: TradePrint | None = None,
    ) -> ContinuousTradeMatch:
        from tivelo_contracts.trade_prints import TradePrint, TradePrintQuery

        account = self._account
        snapshot_id, _, _, dataset = self._pin
        key = instrument.instrument_id
        if confirmed_at < at:
            raise TiveloError(ErrorCode.FUTURE_INFORMATION, "Confirmation precedes the trade")
        day = at.astimezone(ZoneInfo(instrument.timezone)).date()
        query = TradePrintQuery(
            dataset=dataset,
            trading_date=day,
            sequence=sequence,
            instrument_ids=(key,),
            start=at,
            end=at + timedelta(microseconds=1),
            as_of=confirmed_at,
        )
        if query.sequence is None:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "A trade replay requires an exchange sequence"
            )
        identity = day, query.sequence
        previous = account._last_trade_matches.get(key)
        if previous is not None:
            last_identity, last_instrument, last_reference, receipt = previous
            if identity == last_identity:
                if (at, confirmed_at, instrument, reference) != (
                    receipt.trade.event_at,
                    receipt.confirmed_at,
                    last_instrument,
                    last_reference,
                ):
                    raise TiveloError(
                        ErrorCode.DATA_CONFLICT, "Trade replay inputs differ from the prior match"
                    )
                return receipt
            if identity < last_identity or at < receipt.trade.event_at:
                raise TiveloError(
                    ErrorCode.FUTURE_INFORMATION, "A trade sequence cannot be replayed out of order"
                )
        account._time(confirmed_at)
        records = self._reader.trade_prints(query) if record is None else (record,)
        if not records:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Trade print was not known at confirmation"
            )
        if len(records) != 1:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Ambiguous trade print at confirmation")
        trade = TradePrint.model_validate(records[0].model_dump())
        if (
            (trade.instrument_id, trade.trading_date, trade.sequence, trade.event_at)
            != (key, day, query.sequence, at)
            or trade.quote_currency != account.currency
            or trade.session_id != self._market.continuous_session_id(at)
            or trade.source_id not in version.contents.source_ids
        ):
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Trade print does not match the requested market event"
            )
        if trade.available_at > confirmed_at:
            raise TiveloError(ErrorCode.FUTURE_INFORMATION, "Trade print is not yet visible")
        if (
            trade.aggressor_side == "unknown"
            or trade.quantity_unit != "share"
            or trade.quantity != trade.quantity.to_integral_value()
        ):
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Trade matching needs direction and whole shares"
            )
        terms = self._rules.trading_terms(instrument, at=at)
        bounds = self._rules.price_bounds(instrument, reference, at=at)
        with localcontext(Context(prec=96)):
            if trade.raw_price % terms.price_tick or (
                bounds.state == "limited" and not bounds.lower <= trade.raw_price <= bounds.upper
            ):
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Trade price violates the tick or daily limits"
                )
            fills = self._apply_trade(trade, instrument, bounds, terms.price_tick, confirmed_at)
        receipt = ContinuousTradeMatch(snapshot_id, dataset, trade, fills, confirmed_at)
        account._last_trade_matches[key] = (identity, instrument, reference, receipt)
        return receipt

    def _apply_trade(
        self,
        trade: TradePrint,
        instrument: Instrument,
        bounds: PriceBounds,
        tick: Decimal,
        confirmed_at: datetime,
    ) -> tuple[OrderEvent, ...]:
        account = self._account
        at, liquidity = trade.event_at, trade.quantity
        side = "buy" if trade.aggressor_side == "sell" else "sell"
        key = trade.instrument_id

        def crosses(price: Decimal | None) -> bool:
            if price is None or price % tick:
                return False
            if side == "buy":
                return trade.raw_price < price and (
                    bounds.state != "limited" or price < bounds.upper
                )
            return trade.raw_price > price and (bounds.state != "limited" or price > bounds.lower)

        orders = [
            o
            for o in account._pending.values()
            if o.signal.instrument_id == key
            and o.signal.side == side
            and o.expires_at is not None
            and o.submitted_at < at < o.expires_at
            and crosses(o.raw_limit)
        ]
        later_cash_change = False
        later_buy_reserve = Decimal(0)
        if confirmed_at > at:
            funding_at = account._funding_changed_at
            later_cash_change = funding_at is not None and funding_at > at
            # The account appends expiry and report events in confirmation order.
            for event in reversed(account._events):
                if event.event_at <= at:
                    break
                if event.status == "accepted" and event.side == "buy":
                    later_buy_reserve += account._submitted[
                        event.instrument_id, event.decision_at
                    ].reserved_cash
                if event.status not in ("cancelled", "expired"):
                    continue
                later_cash_change |= event.side == "buy"
                if (
                    event.instrument_id == key
                    and event.side == side
                    and event.submitted_at < at
                    and at < account._submitted[key, event.decision_at].expires_at
                    and crosses(event.raw_limit_price)
                ):
                    raise TiveloError(
                        ErrorCode.DATA_CONFLICT, "Trade precedes an already confirmed order closure"
                    )
            if (
                orders
                and account.order_expiry == "end_of_day"
                and any(order.expires_at <= confirmed_at for order in orders)
            ):
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT, "Trade confirmation crosses automatic expiry"
                )
            record_at = account._last_entitlement_at.get(key)
            if orders and record_at is not None and at <= record_at:
                raise TiveloError(ErrorCode.DATA_CONFLICT, "Trade precedes captured ownership")
        if not orders:
            account._time(confirmed_at, advance=True)
            return ()
        self._market.cash_limit_deadline(instrument, side, at=at)
        event_start = len(account._events)
        account._time(confirmed_at, advance=True)
        fill_start = len(account._events)
        if confirmed_at > at:
            later_cash_change |= any(
                event.side == "buy" and event.event_at > at
                for event in account._events[event_start:fill_start]
            )
        free = account.snapshot(confirmed_at).cash[0].available
        for order in sorted(orders, key=lambda o: o.raw_limit, reverse=side == "buy"):
            price = order.raw_limit
            quantity = min(liquidity, order.remaining_quantity)
            fees = self._fee(instrument, side, quantity, price, at)
            reserve = Decimal(0)
            if side == "buy":
                remainder = order.remaining_quantity - quantity
                if remainder:
                    reserve = (
                        remainder * price + self._fee(instrument, side, remainder, price, at).total
                    )
            debit = fees.total + (fees.notional if side == "buy" else -fees.notional)
            room = free + order.reserved_cash
            if later_cash_change and debit + reserve > order.reserved_cash:
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT,
                    "Trade needs additional cash after a later account event",
                )
            if debit + reserve > room:
                if debit + reserve <= room + later_buy_reserve:
                    raise TiveloError(
                        ErrorCode.DATA_CONFLICT, "Trade cash was reserved after execution"
                    )
                break
            account._fill(
                order,
                instrument,
                price,
                fees,
                confirmed_at,
                self._rules,
                quantity=quantity,
                remaining_reserve=reserve,
                filled_at=at,
            )
            balance = room - debit - reserve
            if balance != free:
                # Apply costs in execution order. Credits become usable at confirmation.
                account._record_funding_change(confirmed_at if balance > free else at)
            free = balance
            liquidity -= quantity
            if not liquidity:
                break
        return tuple(account._events[fill_start:])

    def _fee(self, instrument, side, quantity, price, at) -> FeeQuote:
        fees = self._rules.fee_breakdown(instrument, side, quantity, price, at=at)
        if fees.currency != self._account.currency or fees.notional != quantity * price:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Fee quote differs from the order amount")
        return fees

    def _review_resources(self, signal, quantity, instrument, raw_limit, at):
        binding = bind_order_rules(self._rules, instrument, "limit", at=at)
        if binding is None:
            return "unsupported_order_type", Decimal(0)
        portfolio = self._account.snapshot(at)
        available = None
        if signal.side == "sell":
            available = next(
                (
                    p.sellable_quantity
                    for p in portfolio.positions
                    if p.instrument_id == instrument.instrument_id
                ),
                Decimal(0),
            )
            if quantity > available:
                return "insufficient_sellable_quantity", Decimal(0)
        if binding.quantity(signal.side, quantity, available=available) != quantity:
            return "invalid_quantity", Decimal(0)
        if signal.side == "sell":
            return None, Decimal(0)
        with localcontext(Context(prec=96)):
            fees = self._fee(instrument, "buy", quantity, raw_limit, at)
            cash = portfolio.cash[0]
            reserve = fees.notional + fees.total
            if reserve > cash.available:
                return "insufficient_cash", Decimal(0)
            return None, reserve


class ContinuousTradeReplay:
    """Deliver the known prefix of a source's ordered print index for one window."""

    def __init__(
        self,
        admission: ContinuousLimitAdmission,
        query: TradePrintQuery,
        *,
        sequences: tuple[int, ...],
        instrument: Instrument,
        reference: ReferencePrice | None,
    ):
        account = admission._account
        if account.order_expiry != "confirmed_end_of_day":
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Buffered trades need confirmed order expiry"
            )
        version = admission._trade_version(instrument, start=query.start, end=query.end)
        if len(version.contents.source_ids) != 1:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "An ordered trade index needs a single source"
            )
        from pydantic import TypeAdapter, ValidationError
        from tivelo_contracts.trade_prints import TradeSequence

        key = instrument.instrument_id
        zone = ZoneInfo(instrument.timezone)
        if (
            query.dataset != admission._pin[3]
            or query.instrument_ids != (key,)
            or query.sequence is not None
            or query.start.astimezone(zone).date() != query.trading_date
            or (query.end - timedelta(microseconds=1)).astimezone(zone).date() != query.trading_date
        ):
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Trade index does not match the replay window"
            )
        try:
            sequences = TypeAdapter(tuple[TradeSequence, ...]).validate_python(sequences)
        except ValidationError as exc:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid source trade sequence") from exc
        if not sequences or any(left >= right for left, right in pairwise(sequences)):
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Source sequences must be nonempty and increasing"
            )
        self._last_match = account._last_trade_matches.get(key)
        if (
            self._last_match is not None
            and (query.trading_date, sequences[0]) <= self._last_match[0]
        ):
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Trade window overlaps delivered sequences")
        self._admission, self._query = admission, query
        self._instrument, self._reference = instrument, reference
        self._sequences, self._index = sequences, frozenset(sequences)
        self._next = 0
        self._at: datetime | None = None
        self._receipts: tuple[ContinuousTradeMatch, ...] = ()

    def advance(
        self, *, at: datetime, require_complete: bool = False
    ) -> tuple[ContinuousTradeMatch, ...]:
        """Deliver currently visible reports up to the first expected but unknown sequence."""
        at = utc_datetime(at)
        if at < (self._at or self._query.as_of):
            raise TiveloError(ErrorCode.FUTURE_INFORMATION, "Replay knowledge time cannot rewind")
        admission, query = self._admission, self._query
        account = admission._account
        key = self._instrument.instrument_id
        version = admission._trade_version(self._instrument, start=query.start, end=query.end)
        if account._last_trade_matches.get(key) is not self._last_match:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Trade replay cursor changed")
        if self._at == at:
            if require_complete:
                self._check_complete(at, self._next)
            return self._receipts

        account._time(at)
        rows = admission._reader.trade_prints(query.model_copy(update={"as_of": at}))
        visible = {row.sequence: row for row in rows}
        if (
            len(visible) != len(rows)
            or not visible.keys() <= self._index
            or any(
                row.instrument_id != key
                or row.trading_date != query.trading_date
                or not query.start <= row.event_at < query.end
                or row.source_id not in version.contents.source_ids
                for row in rows
            )
        ):
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Trade data differs from the source index")
        if any(row.available_at > at for row in rows):
            raise TiveloError(ErrorCode.FUTURE_INFORMATION, "Trade reader returned future reports")
        ready = []
        for cursor in range(self._next, len(self._sequences)):
            trade = visible.get(self._sequences[cursor])
            if trade is None:
                break
            ready.append(trade)
        next_position = self._next + len(ready)
        if require_complete:
            self._check_complete(at, next_position)
        if ready:
            with admission._trade_transaction(key):
                receipts = tuple(
                    admission._match_trade(
                        version,
                        at=trade.event_at,
                        sequence=trade.sequence,
                        instrument=self._instrument,
                        reference=self._reference,
                        confirmed_at=at,
                        record=trade,
                    )
                    for trade in ready
                )
        else:
            account._time(at, advance=True)
            receipts = ()
        self._last_match = account._last_trade_matches.get(key)
        self._next, self._at, self._receipts = next_position, at, receipts
        return receipts

    def _check_complete(self, at: datetime, position: int) -> None:
        if at < self._query.end or position != len(self._sequences):
            raise TiveloError(ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Trade window is incomplete")
