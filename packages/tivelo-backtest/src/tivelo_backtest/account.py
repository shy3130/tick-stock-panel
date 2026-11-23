"""Exact cash-equity matching and event ledger, separate from legacy curves.

The coordinator owns the clock, data visibility and strategy. This kernel receives
explicit raw opening prices and historical instrument/rule/reference records. It
does no I/O, price adjustment or strategy evaluation. Cash is available for trading
immediately after a sale; withdrawal settlement and margin are not this model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Literal

from pydantic import TypeAdapter
from tivelo_contracts.base import (
    Contract,
    Currency,
    NonNegativeDecimal,
    PositiveDecimal,
    fingerprint,
    utc_datetime,
)
from tivelo_contracts.data import CorporateAction, DatasetReference, Instrument
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import (
    CashBalance,
    DefinitionReference,
    EquityPoint,
    LedgerEntry,
    OrderEvent,
    Portfolio,
    Position,
    Signal,
)
from tivelo_contracts.market import FeeQuote, ReferencePrice
from tivelo_contracts.protocols import ExecutionRules, PriceViewProvider

from .order_rules import bind_order_rules

_POSITIVE = TypeAdapter(PositiveDecimal)
_NONNEGATIVE = TypeAdapter(NonNegativeDecimal)
_QUANTUM = Decimal("0.000000000001")


@dataclass(frozen=True)
class _Lot:
    quantity: Decimal
    cost: Decimal
    sellable_at: datetime


@dataclass(frozen=True)
class ContinuousSubmission:
    event: OrderEvent
    price_check: Contract


@dataclass(frozen=True)
class _Order:
    order_id: str
    signal: Signal
    quantity: Decimal
    submitted_at: datetime
    eligible_at: datetime
    raw_limit: Decimal | None
    expires_at: datetime | None = None
    reserved_cash: Decimal = Decimal(0)
    filled_quantity: Decimal = Decimal(0)
    admission: ContinuousSubmission | None = None
    admission_inputs: tuple[Instrument, ReferencePrice | None] | None = None

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity


OrderExpiry = Literal["next_session", "good_till_cancelled", "end_of_day", "confirmed_end_of_day"]


class CashEquityPolicy(Contract):
    """Immutable account semantics included in a run's account-profile reference."""

    currency: Currency
    order_expiry: OrderExpiry = "next_session"
    dividend_treatment: Literal["reject", "gross_before_personal_tax"] = "reject"
    cost_allocation: Literal["fifo_decimal12_half_even"] = "fifo_decimal12_half_even"
    sale_cash: Literal["immediately_reinvestable"] = "immediately_reinvestable"
    bound_execution: Literal["block_buy_at_upper_sell_at_lower"] = (
        "block_buy_at_upper_sell_at_lower"
    )
    corporate_action_orders: Literal["cancel"] = "cancel"


class CashEquityAccount:
    accounting_model = "event_ledger"

    def __init__(
        self,
        currency: str,
        initial_capital: Decimal | str,
        *,
        order_expiry: OrderExpiry = "next_session",
        dividend_treatment: Literal["reject", "gross_before_personal_tax"] = "reject",
    ):
        self._initial_capital = _POSITIVE.validate_python(initial_capital)
        if order_expiry not in (
            "next_session",
            "good_till_cancelled",
            "end_of_day",
            "confirmed_end_of_day",
        ) or dividend_treatment not in ("reject", "gross_before_personal_tax"):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Unsupported account policy")
        self._policy = CashEquityPolicy(
            currency=currency, order_expiry=order_expiry, dividend_treatment=dividend_treatment
        )
        self._reference = DefinitionReference(
            definition_id="tivelo.account.cash-equity",
            version={"end_of_day": "2", "confirmed_end_of_day": "3"}.get(order_expiry, "1"),
            sha256=fingerprint(self._policy),
            parameters_json=self._policy.model_dump_json(),
        )
        self._continuous_orders = order_expiry in ("end_of_day", "confirmed_end_of_day")
        self._cash = self.initial_capital
        self._lots: dict[str, tuple[_Lot, ...]] = {}
        self._pending: dict[str, _Order] = {}
        self._submitted: dict[tuple[str, datetime], _Order] = {}
        self._continuous_pin: (
            tuple[str, DatasetReference, DefinitionReference, DatasetReference | None] | None
        ) = None
        self._last_trade_matches = {}
        self._events: list[OrderEvent] = []
        self._ledger: list[LedgerEntry] = []
        self._funding_changed_at: datetime | None = None
        self._clock: datetime | None = None
        self._finished = False
        self._entitlements: dict[tuple[str, str], tuple[str, Decimal]] = {}
        self._last_entitlement_at: dict[str, datetime] = {}
        self._applied: dict[tuple[str, str], str] = {}
        self._receivables: dict[tuple[str, str], Decimal] = {}
        self._paid: set[tuple[str, str]] = set()

    @property
    def policy(self) -> CashEquityPolicy:
        return self._policy

    @property
    def reference(self) -> DefinitionReference:
        return self._reference

    @property
    def currency(self) -> str:
        return self.policy.currency

    @property
    def initial_capital(self) -> Decimal:
        return self._initial_capital

    @property
    def order_expiry(self) -> OrderExpiry:
        return self.policy.order_expiry

    @property
    def dividend_treatment(self) -> str:
        return self.policy.dividend_treatment

    @property
    def orders(self) -> tuple[OrderEvent, ...]:
        return tuple(self._events)

    @property
    def ledger(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._ledger)

    @property
    def diagnostics(self) -> tuple[str, ...]:
        return (
            ("gross_dividends_before_personal_tax",)
            if self.dividend_treatment == "gross_before_personal_tax"
            else ()
        )

    def _time(self, at: datetime, *, advance=False) -> datetime:
        at = utc_datetime(at)
        if self._clock is not None and at < self._clock:
            raise TiveloError(ErrorCode.FUTURE_INFORMATION, "Account clock cannot move backwards")
        if advance:
            if self._finished:
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Account execution has finished")
            if self.order_expiry == "end_of_day":
                expired = (
                    order
                    for order in self._pending.values()
                    if order.expires_at is not None and order.expires_at <= at
                )
                for order in sorted(expired, key=lambda order: order.expires_at):
                    self._emit(order, "expired", at=order.expires_at, reason="trading_day_ended")
                    self._pending.pop(order.order_id)
            self._clock = at
        return at

    def _record_funding_change(self, at: datetime) -> None:
        if self._funding_changed_at is None or self._funding_changed_at < at:
            self._funding_changed_at = at

    def _event(
        self,
        order: _Order,
        status: str,
        *,
        at: datetime,
        reason=None,
        price=None,
        fees: FeeQuote | None = None,
        filled_quantity: Decimal = Decimal(0),
        filled_at: datetime | None = None,
    ) -> OrderEvent:
        return OrderEvent(
            order_id=order.order_id,
            instrument_id=order.signal.instrument_id,
            side=order.signal.side,
            status=status,
            decision_at=order.signal.decision_at,
            submitted_at=order.submitted_at,
            eligible_at=order.eligible_at,
            event_at=at,
            requested_quantity=order.quantity,
            filled_at=(filled_at or at) if filled_quantity else None,
            filled_quantity=filled_quantity,
            raw_fill_price=price,
            fee=fees.total if fees else Decimal(0),
            fee_components=fees.components if fees else (),
            currency=self.currency,
            reason=reason,
            order_type=order.signal.order_type,
            raw_limit_price=order.raw_limit,
        )

    def _emit(self, order: _Order, status: str, **event_fields):
        self._events.append(self._event(order, status, **event_fields))

    def _raw_limit(self, signal: Signal, price_view: PriceViewProvider | None) -> Decimal | None:
        if signal.limit_price is None:
            return None
        if price_view is None or signal.limit_price.scale.currency != self.currency:
            raise TiveloError(
                ErrorCode.PRICE_BASIS_UNPROVEN,
                "Limit price needs its controlled scale conversion",
            )
        return _POSITIVE.validate_python(
            price_view.to_raw(signal.limit_price, decision_at=signal.decision_at)
        )

    def _record_submission(
        self, order: _Order, *, reason: str | None = None, price_check: Contract | None = None
    ) -> _Order:
        at = order.submitted_at
        created = self._event(order, "created", at=at)
        outcome = self._event(
            order, "accepted" if reason is None else "rejected", at=at, reason=reason
        )
        if price_check is not None:
            order = replace(
                order, admission=ContinuousSubmission(event=outcome, price_check=price_check)
            )
        self._time(at, advance=True)
        self._events.extend((created, outcome))
        self._submitted[order.signal.instrument_id, order.signal.decision_at] = order
        if reason is None:
            self._pending[order.order_id] = order
        return order

    def submit(
        self,
        signal: Signal,
        quantity: Decimal,
        *,
        eligible_at: datetime,
        price_view: PriceViewProvider | None = None,
    ) -> str:
        if self._continuous_orders:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Continuous orders need witnessed admission"
            )
        eligible_at = utc_datetime(eligible_at)
        if eligible_at <= signal.decision_at:
            raise TiveloError(
                ErrorCode.FUTURE_INFORMATION, "A close signal must execute strictly later"
            )
        quantity = _POSITIVE.validate_python(quantity)
        raw_limit = self._raw_limit(signal, price_view)
        key = signal.instrument_id, signal.decision_at
        previous = self._submitted.get(key)
        if previous is not None:
            if (previous.signal, previous.quantity, previous.eligible_at, previous.raw_limit) != (
                signal,
                quantity,
                eligible_at,
                raw_limit,
            ):
                raise TiveloError(
                    ErrorCode.DATA_CONFLICT,
                    "A decision was resubmitted with different order contents",
                )
            return previous.order_id
        order = _Order(
            f"order:{len(self._submitted) + 1}",
            signal,
            quantity,
            signal.decision_at,
            eligible_at,
            raw_limit,
        )
        return self._record_submission(order).order_id

    def cancel_order(self, order_id: str, *, at: datetime) -> bool:
        """Apply an effective cancellation event and release the simulated reservation."""
        if not self._continuous_orders:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Order cancellation requires continuous admission"
            )
        order = self._pending.get(order_id)
        if order is None:
            return False
        at = self._time(at, advance=True)
        if order.expires_at is not None and at >= order.expires_at:
            return False
        self._pending.pop(order_id)
        self._emit(order, "cancelled", at=at, reason="order_cancelled")
        return True

    def confirm_expiry(self, order_id: str, *, at: datetime) -> bool:
        """Release the remainder after the replay has drained reports up to day close."""
        if self.order_expiry != "confirmed_end_of_day":
            raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "Order expiry is automatic")
        order = self._pending.get(order_id)
        if order is None:
            return False
        at = utc_datetime(at)
        if order.expires_at is None or at < order.expires_at:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Order expiry precedes day close")
        self._time(at, advance=True)
        self._emit(order, "expired", at=at, reason="trading_day_ended")
        self._pending.pop(order_id)
        return True

    def _blocked(self, order: _Order, at: datetime, reason: str):
        terminal = self.order_expiry == "next_session"
        self._emit(order, "expired" if terminal else "accepted", at=at, reason=reason)
        if terminal:
            self._pending.pop(order.order_id)

    def match_open(
        self,
        *,
        at: datetime,
        instruments: Mapping[str, Instrument],
        raw_prices: Mapping[str, Decimal],
        references: Mapping[str, ReferencePrice],
        rules: ExecutionRules,
        slippage_bps: Decimal | str = "0",
    ) -> None:
        if self._continuous_orders or getattr(rules, "requires_order_book", False) is True:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Continuous orders cannot use daily opening fills"
            )
        at = self._time(at, advance=True)
        slippage = _NONNEGATIVE.validate_python(slippage_bps)
        if slippage >= 10000:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Slippage must be below 100 percent")
        # Sells release trading cash before buys; ties never depend on submission/file order.
        pending = sorted(
            self._pending.values(),
            key=lambda order: (
                order.signal.side != "sell",
                order.signal.score.copy_negate(),
                order.signal.instrument_id,
                order.order_id,
            ),
        )
        with localcontext(Context(prec=96)):
            for order in pending:
                if at < order.eligible_at:
                    continue
                if self.order_expiry == "next_session" and at > order.eligible_at:
                    self._blocked(order, at, "missed_eligible_open")
                    continue
                key = order.signal.instrument_id
                security = instruments.get(key)
                if security is None:
                    raise TiveloError(
                        ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                        "Historical instrument state is missing",
                    )
                if security.instrument_id != key or security.asset_class not in ("equity", "fund"):
                    raise TiveloError(
                        ErrorCode.UNSUPPORTED_ASSET,
                        "Cash equity account cannot execute this instrument",
                    )
                if (
                    security.quote_currency != self.currency
                    or security.settlement_currency != self.currency
                ):
                    raise TiveloError(
                        ErrorCode.UNSUPPORTED_CAPABILITY,
                        "Multi-currency settlement needs another account model",
                    )
                if (
                    security.available_at > at
                    or security.valid_from > at
                    or (security.valid_to is not None and at >= security.valid_to)
                ):
                    raise TiveloError(
                        ErrorCode.FUTURE_INFORMATION, "Instrument state is not known/effective"
                    )
                if not security.tradable:
                    self._blocked(order, at, "not_tradable")
                    continue
                if not rules.is_open(security, at=at):
                    self._blocked(order, at, "outside_trading_session")
                    continue
                if key not in raw_prices:
                    self._blocked(order, at, "no_opening_quote")
                    continue
                quote = _POSITIVE.validate_python(raw_prices[key])
                term = rules.trading_terms(security, at=at)
                order_rules = bind_order_rules(
                    rules,
                    security,
                    order.signal.order_type,
                    at=at,
                    trading_terms=term,
                )
                if order_rules is None:
                    self._blocked(order, at, "unsupported_order_type")
                    continue
                if quote % term.price_tick:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Raw execution price violates the market tick"
                    )
                if order.raw_limit is not None and order.raw_limit % term.price_tick:
                    self._blocked(order, at, "invalid_limit_tick")
                    continue
                bounds = rules.price_bounds(security, references.get(key), at=at)
                price = quote * (
                    1 + (slippage if order.signal.side == "buy" else -slippage) / 10000
                )
                price = (price / term.price_tick).to_integral_value(
                    rounding=ROUND_CEILING if order.signal.side == "buy" else ROUND_DOWN
                ) * term.price_tick
                if price <= 0:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID,
                        "Slippage produces a nonpositive execution price",
                    )
                if bounds.state == "limited":
                    assert bounds.lower is not None and bounds.upper is not None
                    if quote < bounds.lower or quote > bounds.upper:
                        raise TiveloError(
                            ErrorCode.CONTRACT_INVALID,
                            "Raw execution price lies outside exchange bounds",
                        )
                    if (
                        order.raw_limit is not None
                        and not bounds.lower <= order.raw_limit <= bounds.upper
                    ):
                        self._blocked(order, at, "limit_price_out_of_bounds")
                        continue
                    if order.signal.side == "buy" and price >= bounds.upper:
                        self._blocked(order, at, "limit_up")
                        continue
                    if order.signal.side == "sell" and price <= bounds.lower:
                        self._blocked(order, at, "limit_down")
                        continue
                if order.raw_limit is not None and (
                    (order.signal.side == "buy" and price > order.raw_limit)
                    or (order.signal.side == "sell" and price < order.raw_limit)
                ):
                    self._blocked(order, at, "limit_price_not_met")
                    continue
                available = sum(
                    (lot.quantity for lot in self._lots.get(key, ()) if lot.sellable_at <= at),
                    Decimal(0),
                )
                legal_quantity = order_rules.quantity(
                    order.signal.side,
                    order.quantity,
                    available=available if order.signal.side == "sell" else None,
                )
                if legal_quantity != order.quantity:
                    self._blocked(
                        order,
                        at,
                        "insufficient_sellable_quantity"
                        if order.signal.side == "sell" and order.quantity > available
                        else "invalid_quantity",
                    )
                    continue
                fees = rules.fee_breakdown(
                    security, order.signal.side, order.quantity, price, at=at
                )
                notional = price * order.quantity
                if fees.currency != self.currency or fees.notional != notional:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Fee quote uses another currency or notional"
                    )
                debit = (
                    notional + fees.total if order.signal.side == "buy" else fees.total - notional
                )
                if debit > self._cash:
                    self._blocked(order, at, "insufficient_cash")
                    continue
                self._fill(order, security, price, fees, at, rules)

    def _fill(
        self,
        order: _Order,
        security: Instrument,
        price: Decimal,
        fees: FeeQuote,
        at: datetime,
        rules: ExecutionRules,
        *,
        quantity: Decimal | None = None,
        remaining_reserve: Decimal = Decimal(0),
        filled_at: datetime | None = None,
    ):
        quantity = order.remaining_quantity if quantity is None else quantity
        if not 0 < quantity <= order.remaining_quantity:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Fill quantity exceeds the order remainder"
            )
        filled_at = filled_at or at
        key = security.instrument_id
        if order.signal.side == "buy":
            unlock = utc_datetime(rules.sellable_at(security, filled_at))
            if unlock < filled_at:
                raise TiveloError(ErrorCode.CONTRACT_INVALID, "Settlement precedes execution")
            cash_delta = -(price * quantity + fees.total)
            lots = (*self._lots.get(key, ()), _Lot(quantity, -cash_delta, unlock))
            quantity_delta = quantity
        else:
            cash_delta = price * quantity - fees.total
            remaining, revised = quantity, []
            for lot in self._lots.get(key, ()):
                sold = min(lot.quantity, remaining) if lot.sellable_at <= filled_at else Decimal(0)
                remaining -= sold
                if sold < lot.quantity:
                    cost = (
                        lot.cost
                        if sold == 0
                        else (lot.cost * (lot.quantity - sold) / lot.quantity).quantize(
                            _QUANTUM, rounding=ROUND_HALF_EVEN
                        )
                    )
                    revised.append(replace(lot, quantity=lot.quantity - sold, cost=cost))
            if remaining:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Rule provider overstates sellable quantity"
                )
            lots, quantity_delta = tuple(revised), -quantity
        cash = _NONNEGATIVE.validate_python(self._cash + cash_delta)
        entry = LedgerEntry(
            entry_id=f"ledger:{len(self._ledger) + 1}",
            at=at,
            kind=order.signal.side,
            reference_id=order.order_id,
            instrument_id=key,
            currency=self.currency,
            cash_delta=cash_delta,
            quantity_delta=quantity_delta,
        )
        filled = order.filled_quantity + quantity
        complete = filled == order.quantity
        self._emit(
            order,
            "filled" if complete else "partially_filled",
            at=at,
            price=price,
            fees=fees,
            filled_quantity=filled,
            filled_at=filled_at,
        )
        self._cash, self._lots[key] = cash, lots
        self._ledger.append(entry)
        if complete:
            self._pending.pop(order.order_id)
        else:
            self._pending[order.order_id] = replace(
                order,
                filled_quantity=filled,
                reserved_cash=remaining_reserve,
            )

    def snapshot(self, at: datetime) -> Portfolio:
        at = self._time(at)
        with localcontext(Context(prec=96)):
            available_cash = self._cash
            reserved_shares: dict[str, Decimal] = {}
            if self._continuous_orders:
                for order in self._pending.values():
                    if order.expires_at is None or (
                        self.order_expiry == "end_of_day" and at >= order.expires_at
                    ):
                        continue
                    available_cash -= order.reserved_cash
                    if order.signal.side == "sell":
                        key = order.signal.instrument_id
                        reserved_shares[key] = (
                            reserved_shares.get(key, Decimal(0)) + order.remaining_quantity
                        )
            positions = tuple(
                Position(
                    instrument_id=key,
                    currency=self.currency,
                    quantity=sum((lot.quantity for lot in lots), Decimal(0)),
                    sellable_quantity=sum(
                        (lot.quantity for lot in lots if lot.sellable_at <= at), Decimal(0)
                    )
                    - reserved_shares.get(key, Decimal(0)),
                    cost=sum((lot.cost for lot in lots), Decimal(0)),
                )
                for key, lots in sorted(self._lots.items())
                if lots
            )
            receivable = sum(self._receivables.values(), Decimal(0))
        return Portfolio(
            cash=(
                CashBalance(
                    currency=self.currency,
                    total=self._cash,
                    available=available_cash,
                    receivable=receivable,
                ),
            ),
            positions=positions,
        )

    def mark_to_market(self, at: datetime, raw_prices: Mapping[str, Decimal]) -> EquityPoint:
        at = self._time(at)
        state = self.snapshot(at)
        with localcontext(Context(prec=96)):
            equity = self._cash + state.cash[0].receivable
            for position in state.positions:
                if position.instrument_id not in raw_prices:
                    raise TiveloError(
                        ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                        "Held instrument has no raw valuation price",
                    )
                equity += position.quantity * _POSITIVE.validate_python(
                    raw_prices[position.instrument_id]
                )
        return EquityPoint(at=at, equity=equity, currency=self.currency)

    def finish(self, at: datetime, *, cancelled=False) -> None:
        if self.order_expiry == "confirmed_end_of_day" and self._pending and not cancelled:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Order terminal reports are still pending")
        at = self._time(at, advance=True)
        for order in tuple(self._pending.values()):
            self._emit(
                order,
                "cancelled" if cancelled else "expired",
                at=at,
                reason="run_cancelled" if cancelled else "end_of_run",
            )
        self._pending.clear()
        self._finished = True

    def capture_entitlement(self, action: CorporateAction, *, at: datetime) -> None:
        at = self._time(at, advance=True)
        if action.record_at != at or action.available_at > at or action.cancelled:
            raise TiveloError(
                ErrorCode.FUTURE_INFORMATION, "Entitlement needs a known action at its record time"
            )
        with localcontext(Context(prec=96)):
            quantity = sum(
                (lot.quantity for lot in self._lots.get(action.instrument_id, ())), Decimal(0)
            )
        value = fingerprint(action), quantity
        identity = action.instrument_id, action.action_id
        if identity in self._entitlements and self._entitlements[identity] != value:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Entitlement was already captured with different inputs"
            )
        self._entitlements[identity] = value
        if self._continuous_orders:
            self._last_entitlement_at[action.instrument_id] = at

    def apply_action(self, action: CorporateAction, *, at: datetime) -> None:
        at = self._time(at, advance=True)
        if action.effective_at != at or action.available_at > at:
            raise TiveloError(
                ErrorCode.FUTURE_INFORMATION,
                "Corporate action is not known/effective at this event",
            )
        digest = fingerprint(action)
        identity = action.instrument_id, action.action_id
        if identity in self._applied:
            if self._applied[identity] != digest:
                raise TiveloError(ErrorCode.DATA_CONFLICT, "Applied corporate action was revised")
            return
        if action.cancelled:
            self._applied[identity] = digest
            return
        key = action.instrument_id
        with localcontext(Context(prec=96)):
            if action.action_type == "split":
                assert action.quantity_ratio is not None
                lots = tuple(
                    replace(
                        lot,
                        quantity=_POSITIVE.validate_python(lot.quantity * action.quantity_ratio),
                    )
                    for lot in self._lots.get(key, ())
                )
                if any(lot.quantity != lot.quantity.to_integral_value() for lot in lots):
                    raise TiveloError(
                        ErrorCode.UNSUPPORTED_CAPABILITY,
                        "Fractional split entitlements need a cash-in-lieu policy",
                    )
                original = sum((lot.quantity for lot in self._lots.get(key, ())), Decimal(0))
                if action.record_at is not None and self._entitlements.get(identity) != (
                    digest,
                    original,
                ):
                    raise TiveloError(
                        ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                        "Split entitlement differs from current ownership",
                    )
                delta = sum((lot.quantity for lot in lots), Decimal(0)) - original
                entry = LedgerEntry(
                    entry_id=f"ledger:{len(self._ledger) + 1}",
                    at=at,
                    kind="split",
                    reference_id=action.action_id,
                    instrument_id=key,
                    currency=self.currency,
                    cash_delta="0",
                    quantity_delta=delta,
                )
                self._lots[key] = lots
            elif action.action_type == "cash_dividend":
                if (
                    self.dividend_treatment != "gross_before_personal_tax"
                    or action.currency != self.currency
                ):
                    raise TiveloError(
                        ErrorCode.UNSUPPORTED_CAPABILITY,
                        "Dividend currency/tax treatment is not configured",
                    )
                if (
                    action.record_at is None
                    or action.payment_at is None
                    or not action.record_at < at <= action.payment_at
                ):
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Dividend requires record, ex and payment times"
                    )
                captured = self._entitlements.get(identity)
                if captured is None or captured[0] != digest:
                    raise TiveloError(
                        ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                        "Dividend record-date ownership was not captured",
                    )
                assert action.cash_per_unit is not None
                amount = _NONNEGATIVE.validate_python(captured[1] * action.cash_per_unit)
                entry = LedgerEntry(
                    entry_id=f"ledger:{len(self._ledger) + 1}",
                    at=at,
                    kind="dividend_accrual",
                    reference_id=action.action_id,
                    instrument_id=key,
                    currency=self.currency,
                    cash_delta="0",
                    quantity_delta="0",
                    receivable_delta=amount,
                )
                self._receivables[identity] = amount
            else:
                raise TiveloError(
                    ErrorCode.UNSUPPORTED_CAPABILITY,
                    "Corporate action needs another accounting policy",
                )
        self._ledger.append(entry)
        self._applied[identity] = digest
        for order in tuple(self._pending.values()):
            if order.signal.instrument_id == key:
                self._emit(order, "cancelled", at=at, reason="corporate_action_changed_basis")
                self._pending.pop(order.order_id)

    def pay_dividend(self, action: CorporateAction, *, at: datetime) -> None:
        at = self._time(at, advance=True)
        identity = action.instrument_id, action.action_id
        if action.payment_at != at or self._applied.get(identity) != fingerprint(action):
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Dividend payment has no matching accrued event"
            )
        if identity in self._paid:
            return
        amount = self._receivables.get(identity)
        if amount is None:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Dividend receivable is missing")
        with localcontext(Context(prec=96)):
            cash = _NONNEGATIVE.validate_python(self._cash + amount)
        entry = LedgerEntry(
            entry_id=f"ledger:{len(self._ledger) + 1}",
            at=at,
            kind="dividend_payment",
            reference_id=action.action_id,
            instrument_id=action.instrument_id,
            currency=self.currency,
            cash_delta=amount,
            quantity_delta="0",
            receivable_delta=amount.copy_negate(),
        )
        self._ledger.append(entry)
        self._cash = cash
        self._receivables.pop(identity)
        self._paid.add(identity)
        if self._continuous_orders and amount:
            self._record_funding_change(at)
