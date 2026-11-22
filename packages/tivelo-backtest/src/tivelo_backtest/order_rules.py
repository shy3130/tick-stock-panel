"""Bind one order type without making old rule providers accept new arguments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, localcontext

from pydantic import TypeAdapter
from tivelo_contracts.base import NonNegativeDecimal
from tivelo_contracts.data import Instrument
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.market import OrderQuantityTerms, OrderType, TradingTerms
from tivelo_contracts.protocols import ExecutionRules, OrderExecutionRules

_NONNEGATIVE = TypeAdapter(NonNegativeDecimal)


@dataclass(frozen=True)
class OrderRuleBinding:
    rules: ExecutionRules
    instrument: Instrument
    terms: OrderQuantityTerms
    at: datetime
    typed: bool

    def quantity(
        self, side: str, requested: Decimal, *, available: Decimal | None = None
    ) -> Decimal:
        requested = _NONNEGATIVE.validate_python(requested)
        if side not in ("buy", "sell") or (side == "sell" and available is None):
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid order quantity request")
        if available is not None:
            available = _NONNEGATIVE.validate_python(available)
        if self.typed:
            result = self.rules.order_quantity(
                self.instrument,
                side,
                requested,
                available=available,
                order_type=self.terms.order_type,
                at=self.at,
            )
        elif side == "buy":
            result = self.rules.buy_quantity(self.instrument, requested, at=self.at)
        else:
            result = self.rules.sell_quantity(
                self.instrument, requested, available=available, at=self.at
            )
        result = _NONNEGATIVE.validate_python(result)
        minimum = getattr(self.terms, f"{side}_minimum")
        increment = getattr(self.terms, f"{side}_increment")
        maximum = getattr(self.terms, f"{side}_maximum")
        with localcontext(Context(prec=96)):
            remainder_once = False
            if side == "sell" and self.terms.odd_lot_policy == "remainder_once":
                if minimum != increment:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID,
                        "Remainder policy requires equal sell minimum and increment",
                    )
                remainder_once = (
                    available % increment != 0 and (available - result) % increment == 0
                )
            odd_remaining = (
                side == "sell"
                and self.terms.odd_lot_policy == "all_remaining"
                and result == available
            ) or remainder_once
            if (
                result > requested
                or result > maximum
                or (side == "sell" and result > available)
                or (
                    result
                    and not odd_remaining
                    and (result < minimum or (result - minimum) % increment)
                )
            ):
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Rule provider returned an invalid order quantity"
                )
        return result


def bind_order_rules(
    rules: ExecutionRules,
    instrument: Instrument,
    order_type: OrderType,
    *,
    at: datetime,
    trading_terms: TradingTerms | None = None,
) -> OrderRuleBinding | None:
    """None is an explicitly disallowed type; a missing implementation is an error."""
    term = trading_terms if trading_terms is not None else rules.trading_terms(instrument, at=at)
    typed = isinstance(rules, OrderExecutionRules)
    if term.order_quantity_rules:
        expected = next((q for q in term.order_quantity_rules if q.order_type == order_type), None)
        if expected is None:
            return None
        if not typed:
            raise TiveloError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Order-specific trading terms require the OrderExecutionRules protocol",
            )
    else:
        expected = OrderQuantityTerms(
            order_type=order_type,
            **{
                name: getattr(term, name)
                for name in OrderQuantityTerms.model_fields
                if name != "order_type"
            },
        )
    if typed and rules.quantity_terms(instrument, order_type, at=at) != expected:
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Order quantity terms differ from the locked trading terms"
        )
    return OrderRuleBinding(rules, instrument, expected, at, typed)
