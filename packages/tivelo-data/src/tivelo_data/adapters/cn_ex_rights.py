"""Reconstruct stock ex-rights ratios from observed per-share terms and raw CNY closes."""

from bisect import bisect_left
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from importlib.metadata import version
from math import floor
from typing import NamedTuple

from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_market_cn.legacy.price_limits import board_limit_pct

from ..legacy.frame import halted_daily_values
from .cn_factor_observation import CNFactorObservation
from .stock_history import _release_digest, source_credential


class CNExRightsObservation(CNFactorObservation):
    definitions = {
        "event_ratio": "tivelo.cn.ex-terms.cent-halfup.ratio-dec12.v1",
        "cumulative_forward": "tivelo.cn.ex-terms.cent-halfup.ratio-dec12.cum-f64-dec12.v1",
    }

    def __init__(self, source, config, *, name, transport, resolve_token, now):
        if source.expected_provider != name:
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Stock factor provider differs from request")
        self.config = config
        self.resolve_token = resolve_token
        super().__init__(
            source,
            batch=1,
            now=now,
            version=f"ex-terms.1.{transport}.{version(transport)}",
            baseline_prefix=f"{name}:ex-terms",
            declaration={
                "settings": config.model_dump(),
                "release": _release_digest(name, transport, factors=True),
            },
        )

    def credential(self, context):
        return source_credential(self.config.credential_ref, self.resolve_token, context)


def number(value):
    """Read decimal source numbers without carrying binary arithmetic into half-cent ties."""
    if type(value) in (str, int, float, Decimal) and len(str(value)) <= 128:
        try:
            d = Decimal(str(value))
            if d.is_finite() and abs(d.as_tuple().exponent) <= 128:
                return Fraction(d)
        except InvalidOperation:
            pass
    raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid stock event or raw bar number")


class Terms(NamedTuple):
    cash: Fraction
    bonus: Fraction
    rights: Fraction
    subscription_cost: Fraction


def terms(cash, bonus, rights, price, *, quantity_unit=1):
    cash, bonus, rights, price = map(number, (cash, bonus, rights, price))
    if cash < 0 or rights < 0 or price < 0 or (rights and not price):
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Invalid dividend or rights subscription price"
        )
    return Terms(
        cash / quantity_unit,
        bonus / quantity_unit,
        rights / quantity_unit,
        rights * price / quantity_unit,
    )


def traded_close(opening, high, close, volume, amount):
    opening, high, close, volume, amount = map(number, (opening, high, close, volume, amount))
    if min(opening, high, close, volume, amount) < 0:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Negative raw daily price or volume")
    if halted_daily_values(opening, high, volume, amount):
        return None
    if not 0 < close <= high or not 0 < opening <= high:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid traded raw daily close")
    return close


def event_ratios(symbol, events, closes):
    days = sorted(closes)
    results = {}
    tolerance = number(board_limit_pct(symbol)) + Fraction(2, 100)
    for day, parts in events.items():
        i = bisect_left(days, day)
        label = f"{symbol} {day.isoformat()}"
        if i == 0 or day not in closes:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                f"Missing raw traded close before or on ex-date {label}",
            )
        if len(set(parts)) != len(parts):
            raise TiveloError(ErrorCode.DATA_CONFLICT, f"Duplicate ex-rights terms for {label}")
        previous = Fraction(closes[days[i - 1]])
        current = Fraction(closes[day])
        cash, bonus, rights, cost = map(sum, zip(*parts, strict=True))
        shares = 1 + bonus + rights
        if shares <= 0 or previous <= 0:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, f"Invalid ex-rights terms for {label}")
        ref = (previous - cash + cost) / shares
        cents = floor(ref * 100 + Fraction(1, 2))
        if cents <= 0:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, f"Nonpositive reference price for {label}"
            )
        reference = Fraction(cents, 100)
        if abs(current / reference - 1) > tolerance:
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, f"Raw ex-date close fails price check for {label}"
            )
        # Round the calculated event once, independently of Python's ambient decimal context.
        ticks = round(previous / reference * 10**12)
        if not 0 < ticks < 10**38:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, f"Unrepresentable ex-rights ratio for {label}"
            )
        results[day] = Decimal(f"{ticks}e-12")
    return results
