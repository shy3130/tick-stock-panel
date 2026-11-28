"""Explicit daily signal plans over v1 research frames.

This validates complete signal evaluation, not the point-in-time provenance or
price-scale invariance of arbitrary data/definitions. Python factors retain the
trusted-code boundary of the existing expression compiler.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import islice
from types import MappingProxyType

import polars as pl
from tivelo_contracts.errors import ErrorCode, TiveloError

from .catalog import (
    BUILTIN_CATALOG,
    FactorCatalog,
    factor_calculation_order,
    factor_dependencies,
    factor_warmup_bars,
)
from .dsl import CompiledFormula, compile_formula
from .py_expr import CompiledPyExpr, compile_python_expression
from .scoring import materialize_scoring_columns
from .signals import (
    ALLOWED_FIELDS,
    COLUMN_RE,
    ID_RE,
    TIMEFRAME_DAILY,
    SignalFields,
    _parse_right,
    build_expressions,
    resolve_column,
    validate,
)

_MAX_SIGNALS = 256


def _invalid(message):
    raise TiveloError(ErrorCode.CONTRACT_INVALID, message)


def _check_values(frame, names, strings):
    for name in sorted(names & set(frame.columns) - {"symbol", "date"}):
        column = frame[name]
        if column.dtype == pl.Null:
            continue
        if name in strings:
            if column.dtype != pl.String:
                _invalid("Signal string input has a non-string type")
        elif not (column.dtype.is_numeric() or column.dtype == pl.Boolean):
            _invalid("Signal numeric input has a non-numeric type")
        elif column.dtype.is_numeric() and not column.cast(pl.Float64).is_finite().fill_null(True).all():
            _invalid("Signal numeric input is not finite")


@dataclass(frozen=True)
class DailySignalPlan:
    """Compiled conditions and factor compilers bound to one immutable catalog.

    warmup_bars is the conservative query/preparation requirement. Evaluation
    preserves the original rolling-window math and maps missing values to false;
    the consuming runner decides the separate warmup/trading date boundary.
    """

    catalog: FactorCatalog
    fields: SignalFields
    dependencies: Mapping[str, frozenset[str]]
    warmup_bars: int
    _expressions: Mapping[str, pl.Expr]
    _dsl: Mapping[str, CompiledFormula]
    _python: Mapping[str, CompiledPyExpr]

    def evaluate(self, frame: pl.DataFrame) -> pl.DataFrame:
        if not isinstance(frame, pl.DataFrame):
            _invalid("Signal evaluation requires a Polars DataFrame")
        if not self._expressions:
            return frame
        if not {"symbol", "date"}.issubset(frame.columns):
            _invalid("Daily signal input requires symbol/date grouping keys")
        if set(self._expressions) & set(frame.columns):
            raise TiveloError(ErrorCode.DATA_CONFLICT, "Signal output already exists in the input")
        required = set().union(*self.dependencies.values())
        try:
            keys = frame.select("symbol", "date")
            if keys.null_count().sum_horizontal().item() or frame["symbol"].dtype != pl.String:
                _invalid("Signal grouping keys are null or have an invalid symbol type")
            if keys.n_unique() != frame.height or not keys.equals(keys.sort(["symbol", "date"])):
                _invalid("Signal rows must have unique keys ordered by symbol/date")
            _check_values(
                frame, required | factor_dependencies(required, catalog=self.catalog),
                self.fields.string_fields,
            )
            result = materialize_scoring_columns(
                frame, sorted(required), catalog=self.catalog,
                compile_dsl=self._dsl.__getitem__, compile_python=self._python.__getitem__,
            )
            if not required.issubset(result.columns):
                raise TiveloError(
                    ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                    "A required signal/factor input could not be materialized",
                )
            _check_values(result, required, self.fields.string_fields)
            return result.with_columns([
                expression.fill_null(False).cast(pl.Boolean).alias(name)
                for name, expression in self._expressions.items()
            ])
        except pl.exceptions.PolarsError as exc:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Signal computation failed for the supplied frame"
            ) from exc


def compile_daily_signals(
    definitions: Iterable[dict],
    *,
    catalog: FactorCatalog = BUILTIN_CATALOG,
    string_fields: Iterable[str] = (),
) -> DailySignalPlan:
    """Compile at most 256 definitions; any invalid enabled signal rejects the plan.

    Daily definitions use the existing left/op/right and leftDays/rightDays
    format. Offsets in this explicit API must be JSON integers, constants must
    be finite, and output names cannot shadow inputs. Compatibility builders
    retain their original coercion and skip behavior.
    """
    if not isinstance(catalog, FactorCatalog):
        _invalid("Signal plans require an immutable FactorCatalog")
    try:
        signals = list(islice(definitions, _MAX_SIGNALS + 1))
        fields = SignalFields(
            allowed=ALLOWED_FIELDS | {spec.id for spec in catalog.specs},
            string_fields=frozenset(string_fields),
        )
    except TypeError as exc:
        raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid signal definitions or fields") from exc
    if len(signals) > _MAX_SIGNALS:
        _invalid("Signal definition count exceeds the plan budget")
    if any(not isinstance(name, str) or not name for name in fields.allowed):
        _invalid("Signal field names must be nonempty strings")
    if {"symbol", "date"} & fields.allowed:
        _invalid("Signal fields cannot replace grouping keys")

    identities = set()
    expressions: dict[str, pl.Expr] = {}
    dependencies: dict[str, frozenset[str]] = {}
    warmup = 1
    for signal in signals:
        if not isinstance(signal, dict):
            _invalid("Signal definitions must be objects")
        if "enabled" in signal and type(signal["enabled"]) is not bool:
            _invalid("Signal enabled must be a boolean")
        if signal.get("enabled") is False:
            continue
        if signal.get("timeframe", TIMEFRAME_DAILY) != TIMEFRAME_DAILY:
            raise TiveloError(ErrorCode.UNSUPPORTED_CAPABILITY, "This plan requires daily signals")
        try:
            validate(signal, fields=fields)
            identity = signal["id"]
            name = resolve_column(signal)
            if not ID_RE.fullmatch(identity) or not COLUMN_RE.fullmatch(name):
                _invalid("Signal identifiers must be portable column names")
            if identity in identities or name in expressions or name in fields.allowed | {"symbol", "date"}:
                _invalid("Signal identity or output column conflicts")
            for condition in signal["conditions"]:
                for key in ("leftDays", "rightDays"):
                    if type(condition.get(key, 0)) is not int:
                        _invalid("Signal offsets must be integers")
                left = condition["left"]
                kind, right = _parse_right(
                    condition["right"], string_mode=left in fields.string_fields, fields=fields,
                )
                if kind == "const" and not math.isfinite(right):
                    _invalid("Signal constants must be finite")
                if kind == "field" and right in fields.string_fields:
                    _invalid("Numeric signal conditions cannot compare a string field")
                warmup = max(
                    warmup,
                    factor_warmup_bars([left], catalog=catalog) + condition.get("leftDays", 0),
                )
                if kind == "field":
                    warmup = max(
                        warmup,
                        factor_warmup_bars([right], catalog=catalog) + condition.get("rightDays", 0),
                    )
            compiled = build_expressions([signal], fields=fields)
            if set(compiled) != {name}:
                _invalid("Signal compilation did not produce the declared output")
            identities.add(identity)
            expressions[name] = compiled[name]
            dependencies[name] = frozenset(compiled[name].meta.root_names())
        except (ValueError, TypeError, KeyError, pl.exceptions.PolarsError) as exc:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, "Invalid daily signal definition") from exc

    dsl: dict[str, CompiledFormula] = {}
    python: dict[str, CompiledPyExpr] = {}
    for name in factor_calculation_order(set().union(*dependencies.values()), catalog=catalog):
        spec = catalog.get(name)
        if spec is None or spec.kind != "custom":
            continue
        if spec.lang == "dsl":
            compiled = compile_formula(spec.formula_text, catalog=catalog)
            dsl[spec.formula_text] = compiled
        elif spec.lang == "python":
            compiled = compile_python_expression(spec.formula_text, catalog=catalog)
            python[spec.formula_text] = compiled
        else:
            _invalid("Signal factor language is unsupported")
        if not compiled.ok:
            _invalid("A referenced signal factor does not compile")
    return DailySignalPlan(
        catalog, fields, MappingProxyType(dependencies), warmup,
        MappingProxyType(expressions), MappingProxyType(dsl), MappingProxyType(python),
    )
