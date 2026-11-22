"""Conservative positive per-instrument scale algebra over the existing DSL AST.

This proves homogeneity of real expressions, not equality of rounded chart bars.
The admitted executor fixes its numeric semantics by cancelling the unknown anchor
before Float64 evaluation. Unknown columns/operators and mixed units fail closed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction

from tivelo_contracts.errors import ErrorCode, TiveloError

from .dsl import OPERATORS

PRICE_FIELDS = frozenset({"open", "high", "low", "close"})
RAW_FIELDS = frozenset({"volume", "amount"})


def _reject():
    raise TiveloError(
        ErrorCode.PRICE_BASIS_UNPROVEN,
        "Controlled expression has unproved per-instrument price-scale or type semantics",
    )


@dataclass(frozen=True)
class Unit:
    degree: Fraction = Fraction(0)
    boolean: bool = False
    literal_zero: bool = False


def _same(left, right):
    if left.boolean != right.boolean:
        _reject()
    if left.degree == right.degree:
        return Unit(left.degree, left.boolean)
    if left.literal_zero:
        return Unit(right.degree, right.boolean)
    if right.literal_zero:
        return Unit(left.degree, left.boolean)
    _reject()


def expression_unit(node: dict, *, field_units: Mapping[str, Unit] | None = None) -> Unit:
    kind, value, children = node["kind"], node["value"], node["children"]
    if kind == "num":
        if not math.isfinite(value):
            _reject()
        return Unit(literal_zero=value == 0)
    if kind == "col":
        if value in PRICE_FIELDS:
            return Unit(Fraction(1))
        if value in RAW_FIELDS:
            return Unit()
        if field_units is not None and value in field_units:
            return field_units[value]
        _reject()
    args = [expression_unit(child, field_units=field_units) for child in children]
    if kind == "unary":
        if value != "-" or args[0].boolean:
            _reject()
        return args[0]
    if kind == "bin":
        left, right = args
        if value in {"and", "or"}:
            if not left.boolean or not right.boolean:
                _reject()
            return Unit(boolean=True)
        if left.boolean or right.boolean:
            _reject()
        if value in {"+", "-"}:
            return _same(left, right)
        if value in {">", ">=", "<", "<=", "==", "!="}:
            _same(left, right)
            return Unit(boolean=True)
        if value == "*":
            return Unit(left.degree + right.degree)
        if value == "/":
            return Unit(left.degree - right.degree)
        _reject()
    if kind != "call" or value not in OPERATORS:
        _reject()
    args = args[: OPERATORS[value][0]]
    first = args[0]
    if value == "if_else":
        if not first.boolean:
            _reject()
        return _same(args[1], args[2])
    if any(arg.boolean for arg in args):
        _reject()
    if value in {"min", "max"}:
        return _same(*args)
    if value in {
        "abs",
        "ts_mean",
        "ts_std",
        "ts_sum",
        "ts_max",
        "ts_min",
        "ts_delay",
        "ts_delta",
        "ts_quantile",
        "decay_linear",
    }:
        return Unit(first.degree)
    if value in {"sign", "ts_rank", "ts_zscore", "ts_corr"}:
        return Unit()
    if value == "ts_cov":
        return Unit(sum(arg.degree for arg in args))
    if value == "sqrt":
        return Unit(first.degree / 2)
    if value == "power":
        return Unit(first.degree * Fraction(str(node["_constants"]["c"])))
    if value in {"rank", "zscore", "winsorize", "log", "clamp"}:
        # Cross-sectional prices have independent unknown normalizations.
        if first.degree != 0:
            _reject()
        return Unit()
    _reject()
