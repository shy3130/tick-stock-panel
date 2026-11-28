"""Bounded daily strategies with core-verified, anchor-cancelled execution.

The JSON program owns the complete entry/exit/score/selection/weight pipeline.
Format 1.1 admits only sealed snapshot fields in addition to the built-in market inputs.
Canonical inputs are raw price times the visible cumulative factor, never a
fabricated future-qfq Bar. This uses a fixed factor baseline, not a moving anchor.
"""

from __future__ import annotations

import hashlib
import math
from bisect import bisect_right
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from typing import Literal, Self

import polars as pl
from pydantic import Field, model_validator
from tivelo_contracts.base import Contract, UnitRatio, canonical_bytes, fingerprint, parse_json
from tivelo_contracts.definitions import DefinitionBundle, DefinitionFile
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.execution import ScaleProof, Signal, SignalBatch
from tivelo_contracts.extension_fields import FormulaField, validate_formula_fields

from .controlled_fields import complete_formula_inputs
from .daily import StrategyBinding
from .definitions import CheckedDefinitions, DefinitionLoader
from .price_views import _visible_factors
from .research.dsl import FACTOR_COLUMN, CompiledFormula, compile_formula
from .research.scale_analysis import PRICE_FIELDS, RAW_FIELDS, Unit, expression_unit

_NUMERIC_CONTEXT = Context(prec=96, rounding=ROUND_HALF_EVEN)
_SCORE_QUANTUM = Decimal("0.000000000001")


class ControlledProgram(Contract):
    format: Literal["tivelo-controlled-daily"] = "tivelo-controlled-daily"
    format_version: Literal["1.0", "1.1"] = "1.0"
    field_operands: tuple[FormulaField, ...] = Field(
        default=(), max_length=64, exclude_if=lambda values: not values
    )
    missing_fields: Literal["error", "skip_signal"] = Field(
        default="error", exclude_if=lambda v: v == "error"
    )
    numeric_semantics: Literal["anchor_cancelled_float64_v1", "projected_prices_float64_v1"] = (
        "anchor_cancelled_float64_v1"
    )
    entry: str = Field(strict=True, min_length=1, max_length=4096)
    exit: str = Field(strict=True, min_length=1, max_length=4096)
    score: str = Field(strict=True, min_length=1, max_length=4096)
    max_positions: int = Field(default=1, strict=True, ge=1, le=1000)
    weight_per_position: UnitRatio = "1"
    min_history_bars: int = Field(default=1, strict=True, ge=1, le=10000)

    @model_validator(mode="after")
    def weights(self) -> Self:
        if bool(self.field_operands) != (self.format_version == "1.1"):
            raise ValueError("format 1.1 requires sealed field operands")
        if self.missing_fields != "error" and not self.field_operands:
            raise ValueError("a missing-field policy needs bound fields")
        validate_formula_fields(self.field_operands)
        with localcontext(_NUMERIC_CONTEXT):
            if self.weight_per_position <= 0 or self.weight_per_position * self.max_positions > 1:
                raise ValueError("fixed position weights must be positive and total at most one")
        return self

    def proof_for(self, reference) -> ScaleProof:
        return ScaleProof(
            verifier_version="tivelo.scale.controlled.v1.1"
            if self.field_operands
            else "tivelo.scale.controlled.v1",
            field_operands=self.field_operands,
            strategy=reference,
            program_sha256=fingerprint(self),
            numeric_semantics=self.numeric_semantics,
        )


def derive_program_bundle(bundle, derived, *, version_prefix):
    payload = canonical_bytes(derived)
    declared = DefinitionFile(
        path=bundle.contents.entrypoint,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    candidate = DefinitionBundle.create(
        bundle.contents.model_copy(
            update={
                "version": version_prefix + "." + fingerprint(derived),
                "files": (declared,),
            }
        )
    )
    return candidate, candidate.reference(), payload


def _compile(program):
    units = {
        item.alias: Unit(boolean=item.value_type in ("bool", "string"))
        for item in program.field_operands
    }
    formulas = []
    for name in ("entry", "exit", "score"):
        compiled = compile_formula(getattr(program, name), input_columns=frozenset(units))
        if not compiled.ok or compiled.price_ast is None:
            raise TiveloError(ErrorCode.CONTRACT_INVALID, f"Invalid controlled {name} formula")
        unit = expression_unit(compiled.price_ast, field_units=units)
        if unit.degree != 0 or unit.boolean != (name != "score"):
            raise TiveloError(
                ErrorCode.PRICE_BASIS_UNPROVEN, f"Controlled {name} formula is not scale invariant"
            )
        formulas.append(compiled)
    used = set().union(*(formula.dependencies for formula in formulas))
    if units.keys() - used:
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Every sealed field alias must be used by the program"
        )
    return tuple(formulas)


def load_controlled(inspected: CheckedDefinitions) -> StrategyBinding:
    """Validate sealed bytes and compile the exact program; no self-reported proof."""
    DefinitionLoader().validate(inspected)
    node = inspected.nodes[-1]
    if (
        len(inspected.nodes) != 1
        or node.bundle.contents.language != "controlled_daily_v1"
        or node.reference.parameters_json != "{}"
    ):
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID,
            "Controlled v1 requires a single JSON program with all parameters in the sealed file",
        )
    try:
        payload = dict(node.files)[node.bundle.contents.entrypoint]
        if len(payload) > 1_048_576:
            raise ValueError("program byte budget exceeded")
        program = ControlledProgram.model_validate(parse_json(payload.decode("utf-8")))
    except (ValueError, TypeError) as exc:
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID, "Invalid controlled strategy program"
        ) from exc
    formulas = _compile(program)
    proof = program.proof_for(inspected.root)
    implementation = ControlledStrategy(inspected, program, proof, formulas)
    return StrategyBinding(implementation, inspected.root)


def validate_controlled(binding, lock):
    """Recompile verified bytes, so mutable compiler state cannot confer admission."""
    proof = lock.contents.scale_proof
    if proof is None and type(binding.implementation) is not ControlledStrategy:
        return None
    if proof is None or type(binding.implementation) is not ControlledStrategy:
        raise TiveloError(
            ErrorCode.PRICE_BASIS_UNPROVEN, "A controlled proof needs its core executor"
        )
    fresh = load_controlled(binding.implementation.definitions)
    if (
        fresh.reference != binding.reference
        or binding.dependencies
        or fresh.implementation.proof != proof
    ):
        raise TiveloError(
            ErrorCode.PRICE_BASIS_UNPROVEN, "Controlled program differs from its proof"
        )
    check_controlled_spec(lock.contents.spec, proof)
    return fresh.implementation


def check_controlled_spec(spec, proof):
    expected = {(v.dataset_id, v.field_sha256) for v in proof.field_operands}
    actual = {(v.dataset_id, v.field_sha256) for v in spec.extension_fields}
    if actual != expected:
        raise TiveloError(
            ErrorCode.DATA_CONFLICT if expected else ErrorCode.UNSUPPORTED_CAPABILITY,
            "Controlled strategy field operands differ from the requested snapshot fields",
        )
    if spec.price_view.mode == "extension":
        compatible = proof.numeric_semantics == "projected_prices_float64_v1"
    else:
        compatible = (
            proof.numeric_semantics == "anchor_cancelled_float64_v1"
            and spec.price_view.provider_id == f"tivelo.{spec.price_view.mode}"
        )
    if not compatible or spec.execution.order_expiry != "next_session":
        raise TiveloError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "Controlled program requires matching numeric semantics and next-session order expiry",
        )


def _canonical_frame(context, factors, spec, field_history=None):
    bars = context.history
    projected = spec.mode == "extension"
    adjusted = spec.mode in ("qfq", "hfq")
    ids = {bar.instrument_id for bar in bars}
    series = _visible_factors(tuple(factors), ids, spec, context.now) if adjusted else {}
    times = {key: [factor.effective_at for factor in values] for key, values in series.items()}
    if adjusted and spec.anchor_at <= context.now:
        if any(bisect_right(boundaries, spec.anchor_at) == 0 for boundaries in times.values()):
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                "Known anchor precedes the declared cumulative factor history",
            )
    records, identities = [], set()
    for bar in bars:
        identity = bar.instrument_id, bar.trading_date
        if (
            identity in identities
            or bar.quantity_unit != "share"
            or bar.price_basis != ("extension" if projected else "raw")
            or (projected and bar.source_id != spec.provider_id)
        ):
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID,
                "Controlled input has inconsistent share bars or price provenance",
            )
        identities.add(identity)
        multiplier = Decimal(1)
        if adjusted:
            boundaries = times[bar.instrument_id]
            index = bisect_right(boundaries, bar.bar_start) - 1
            if index < 0:
                raise TiveloError(
                    ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Missing visible cumulative factor"
                )
            if index + 1 < len(boundaries) and boundaries[index + 1] < bar.bar_end:
                raise TiveloError(ErrorCode.FACTOR_INCOMPATIBLE, "Adjustment changes inside a bar")
            multiplier = series[bar.instrument_id][index].value
        record = {"symbol": bar.instrument_id, "date": bar.trading_date}
        with localcontext(_NUMERIC_CONTEXT):
            for name in sorted(PRICE_FIELDS | RAW_FIELDS):
                raw = bar.turnover if name == "amount" else getattr(bar, name)
                if raw is None:
                    record[name] = None
                    continue
                value = raw * multiplier if name in PRICE_FIELDS else raw
                converted = float(value)
                if not math.isfinite(converted) or (value != 0 and converted == 0):
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Controlled input exceeds Float64 range"
                    )
                record[name] = converted
        if field_history is not None:
            record.update(field_history.row(bar.trading_date, bar.instrument_id))
        records.append(record)
    schema = {
        "symbol": pl.String,
        "date": pl.Date,
        **{name: pl.Float64 for name in PRICE_FIELDS | RAW_FIELDS},
        **(field_history.schema if field_history is not None else {}),
    }
    return pl.DataFrame(records, schema=schema).sort("symbol", "date")


@dataclass(frozen=True)
class ControlledStrategy:
    definitions: CheckedDefinitions
    program: ControlledProgram
    proof: ScaleProof
    formulas: tuple[CompiledFormula, ...] = field(repr=False)

    def decide(self, context):
        raise TiveloError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "Controlled strategies require core factor-aware execution",
        )

    def decide_with_factors(self, context, *, factors, price_view, field_history=None):
        if self.program.field_operands and (
            field_history is None or field_history.operands != self.program.field_operands
        ):
            raise TiveloError(
                ErrorCode.DATA_CONFLICT, "Missing or mismatched controlled field history"
            )
        frame = _canonical_frame(context, factors, price_view, field_history)
        if not context.openings:
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Controlled decision needs opening states"
            )
        dates = {observation.trading_date for observation in context.openings}
        if len(dates) != 1:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Controlled decision spans multiple trading dates"
            )
        trading_date = next(iter(dates))
        if frame.is_empty():
            return SignalBatch(decision_at=context.now, signals=())
        required = set().union(*(formula.dependencies for formula in self.formulas))
        if any(frame[name].null_count() for name in required & (PRICE_FIELDS | RAW_FIELDS)):
            raise TiveloError(
                ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Controlled input field is unavailable"
            )
        try:
            if field_history is not None:
                frame = frame.with_columns(
                    complete_formula_inputs(self.formulas, field_history.schema).alias(
                        "__fields_ready"
                    )
                )
            for name, formula in zip(("entry", "exit", "score"), self.formulas, strict=True):
                output = formula.frame_transform(frame)
                if output is None or output.height != frame.height:
                    raise TiveloError(
                        ErrorCode.CONTRACT_INVALID, "Controlled formula changed its population"
                    )
                column = output[FACTOR_COLUMN]
                if name != "score" and column.dtype != pl.Boolean:
                    if column.drop_nulls().is_in([0, 1]).not_().any():
                        raise TiveloError(
                            ErrorCode.CONTRACT_INVALID, "Controlled predicate is not Boolean"
                        )
                    column = column.cast(pl.Boolean, strict=True)
                frame = frame.with_columns(column.alias("__controlled_" + name))
            frame = frame.with_columns(pl.col("symbol").cum_count().over("symbol").alias("__bars"))
        except pl.exceptions.PolarsError as exc:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Controlled formula evaluation failed"
            ) from exc
        needed = max(
            self.program.min_history_bars, *(formula.warmup_bars for formula in self.formulas)
        )
        # Compute cross-sectional formulas on all observed rows before trade eligibility filtering.
        states = {item.instrument_id for item in context.openings if item.state == "tradable"}
        states &= {item.instrument_id for item in context.instruments if item.tradable}
        current = frame.filter((pl.col("date") == trading_date) & (pl.col("__bars") >= needed))
        held = {
            position.instrument_id
            for position in context.portfolio.positions
            if position.quantity > 0
        }
        if len(held) > self.program.max_positions:
            raise TiveloError(
                ErrorCode.CONTRACT_INVALID, "Portfolio exceeds controlled position slots"
            )
        candidates, sells = [], []
        for row in current.iter_rows(named=True):
            key = row["symbol"]
            if key not in states:
                continue
            if field_history is not None and not row["__fields_ready"]:
                if self.program.missing_fields == "skip_signal":
                    continue
                raise TiveloError(
                    ErrorCode.DATA_COVERAGE_INSUFFICIENT,
                    f"Required controlled field history is incomplete for {key} on {trading_date}",
                )
            entry, exit_, score = (
                row["__controlled_" + name] for name in ("entry", "exit", "score")
            )
            if (
                type(entry) is not bool
                or type(exit_) is not bool
                or score is None
                or not math.isfinite(score)
            ):
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID,
                    "Controlled output is null, nonfinite or mistyped after warmup",
                )
            try:
                with localcontext(_NUMERIC_CONTEXT):
                    exact_score = Decimal(str(score)).quantize(_SCORE_QUANTUM)
                signal = Signal(
                    instrument_id=key,
                    decision_at=context.now,
                    side="sell" if key in held else "buy",
                    score=exact_score,
                    source_id=self.definitions.root.definition_id,
                    target_weight="0" if key in held else self.program.weight_per_position,
                )
            except (ValueError, InvalidOperation) as exc:
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID, "Controlled score exceeds the output contract"
                ) from exc
            if key in held and exit_:
                sells.append(signal)
            elif key not in held and entry and not exit_:
                candidates.append(signal)
        candidates.sort(key=lambda signal: (signal.score.copy_negate(), signal.instrument_id))
        sells.sort(key=lambda signal: (signal.score.copy_negate(), signal.instrument_id))
        # Selling does not release a slot until execution confirms the position is gone.
        selected = candidates[: self.program.max_positions - len(held)]
        return SignalBatch(decision_at=context.now, signals=tuple((*sells, *selected)))
