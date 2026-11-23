"""Align immutable research values to each session's own decision cutoff."""

import math
from collections import defaultdict
from datetime import date

import polars as pl
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.extension_fields import ExtensionQuery, ExtensionValue, FormulaField


def extension_observations(reader, research, now):
    observations = []
    for reference, dataset, subjects in research:
        requested_ids = set(subjects)
        field = dataset.contents.scope.extension_field
        query = ExtensionQuery(
            dataset_id=reference.dataset_id,
            field_sha256=reference.field_sha256,
            instrument_ids=subjects,
            effective_at=now,
            as_of=now,
        )
        seen = set()
        for item in reader.extension_values(query):
            row = ExtensionValue.model_validate(item.model_dump())
            if (
                not row.matches(field, reference.field_sha256)
                or row.source_id not in dataset.contents.source_ids
                or (row.instrument_id is not None and row.instrument_id not in requested_ids)
                or row.instrument_id in seen
                or row.effective_at > now
                or row.available_at > now
                or (row.valid_until is not None and row.valid_until <= now)
            ):
                raise TiveloError(
                    ErrorCode.CONTRACT_INVALID,
                    "Reader returned a conflicting or unavailable extension value",
                )
            seen.add(row.instrument_id)
            observations.append(row)
    return tuple(observations)


def _scalar(operand: FormulaField, value: ExtensionValue) -> bool | float | None:
    if operand.value_type == "bool":
        return value.bool_value
    if operand.value_type == "string":
        return None if value.text_value is None else value.text_value == operand.equals_text
    number = value.int_value if operand.value_type == "int" else value.decimal_value
    if number is None:
        return None
    converted = float(number)
    if (
        not math.isfinite(converted)
        or (number != 0 and converted == 0)
        or (operand.value_type == "int" and int(converted) != number)
    ):
        raise TiveloError(
            ErrorCode.CONTRACT_INVALID,
            f"Field {operand.alias} exceeds its Float64 representation",
        )
    return converted


class FormulaFieldHistory:
    """One observation of each bound dataset per session, shared by all three formulas."""

    def __init__(self, operands: tuple[FormulaField, ...]):
        self.operands = operands
        self.by_field = defaultdict(list)
        self.schema = {}
        self.by_date = {}
        for operand in operands:
            self.by_field[operand.field_sha256].append(operand)
            self.schema[operand.alias] = (
                pl.Boolean if operand.value_type in ("bool", "string") else pl.Float64
            )

    def observe(self, trading_date: date, values: tuple[ExtensionValue, ...]) -> None:
        day = {op.alias: {} for op in self.operands}
        for value in values:
            for operand in self.by_field[value.field_sha256]:
                day[operand.alias][value.instrument_id] = _scalar(operand, value)
        self.by_date[trading_date] = day

    def row(self, trading_date: date, instrument_id: str):
        # A market-level field has exactly one None subject. Instrument fields never have one.
        return {
            alias: by_subject.get(instrument_id, by_subject.get(None))
            for alias, by_subject in self.by_date[trading_date].items()
        }


def complete_formula_inputs(formulas, aliases):
    """Mark sparse histories without dropping dates or shortening a rolling window."""
    windows = {}
    for formula in formulas:
        for name, lag in formula.input_windows:
            if name in aliases:
                windows[name] = max(windows.get(name, 0), lag)
    checks = []
    for name, lag in windows.items():
        valid = pl.col(name).is_not_null()
        if lag:
            size = lag + 1
            valid = valid.cast(pl.UInt32).rolling_sum(size, min_samples=size).over("symbol") == size
        checks.append(valid)
    return pl.all_horizontal(checks).fill_null(False)
