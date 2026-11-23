"""Stock ex-rights event and fixed-baseline cumulative definitions."""

import math
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from tivelo_contracts.errors import ErrorCode, TiveloError

DEFINITIONS = {
    "event_ratio": "tivelo.cn.v1.ex-factor.event",
    "cumulative_forward": "tivelo.cn.v1.ex-factor.cum-f64-dec12",
}
_QUANTUM = Decimal("0.000000000001")


def _derived_value(product):
    # Match the v1 Float64 running product. Round only the published checkpoint.
    if not math.isfinite(product) or product <= 0 or product >= 1e26:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Cumulative factor exceeds decimal128 range")
    with localcontext(Context(prec=96, rounding=ROUND_HALF_EVEN)):
        value = Decimal.from_float(product).quantize(_QUANTUM)
    if not value:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Cumulative factor falls below precision")
    return value


def factor_values(dates, *, representation, start, baseline_date):
    """Consume unique Beijing ex-dates in order and emit requested checkpoints."""
    if representation == "event_ratio":
        for day, ratio in dates:
            yield day, ratio
        return

    product = 1.0
    carry_written = False
    for day, ratio in dates:
        if day > start and not carry_written:
            yield start, _derived_value(product)
            carry_written = True
        if day > baseline_date:
            product *= float(ratio)
        if day > start:
            yield day, _derived_value(product)
    if not carry_written:
        yield start, _derived_value(product)
