"""Public record schemas and immutable identity fields; no storage implementation."""

import types
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from types import MappingProxyType
from typing import Annotated, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from .data import (
    AdjustmentFactor,
    Bar,
    CorporateAction,
    Instrument,
    OpeningObservation,
    SymbolMapping,
)
from .extension_fields import ExtensionValue
from .order_books import OrderBookObservation
from .trade_prints import TradePrint

RecordType = Literal[
    "bar",
    "instrument",
    "symbol_mapping",
    "corporate_action",
    "adjustment_factor",
    "opening_observation",
    "extension_value",
    "order_book_observation",
    "trade_print",
]
RECORD_MODELS = MappingProxyType(
    {
        "bar": Bar,
        "instrument": Instrument,
        "symbol_mapping": SymbolMapping,
        "corporate_action": CorporateAction,
        "adjustment_factor": AdjustmentFactor,
        "opening_observation": OpeningObservation,
        "extension_value": ExtensionValue,
        "order_book_observation": OrderBookObservation,
        "trade_print": TradePrint,
    }
)
REVISION_KEYS = MappingProxyType(
    {
        "bar": ("instrument_id", "timeframe", "bar_start", "source_id", "revision_id"),
        "instrument": ("instrument_id", "valid_from", "source_id", "revision_id"),
        "symbol_mapping": ("instrument_id", "valid_from", "source_id", "revision_id"),
        "corporate_action": ("instrument_id", "action_id", "source_id", "revision_id"),
        "adjustment_factor": (
            "instrument_id",
            "definition_id",
            "baseline_id",
            "factor_id",
            "source_id",
            "revision_id",
        ),
        "opening_observation": ("instrument_id", "event_at", "source_id", "revision_id"),
        "order_book_observation": ("instrument_id", "event_at", "source_id", "revision_id"),
        "trade_print": ("instrument_id", "trading_date", "sequence", "source_id", "revision_id"),
        "extension_value": (
            "field_sha256",
            "instrument_id",
            "effective_at",
            "source_id",
            "revision_id",
        ),
    }
)
BUSINESS_KEYS = MappingProxyType(
    {
        kind: tuple(key for key in keys if key != "revision_id")
        for kind, keys in REVISION_KEYS.items()
    }
)


@dataclass(frozen=True)
class FactField:
    """Library-neutral physical type: decimals are (38,12), timestamps UTC µs."""

    name: str
    kind: str
    nullable: bool
    children: tuple["FactField", ...] = ()


def _field(name, annotation) -> FactField:
    nullable = False
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if get_origin(annotation) in (Union, types.UnionType):
        args = get_args(annotation)
        nullable = type(None) in args
        remaining = [arg for arg in args if arg is not type(None)]
        if len(remaining) != 1:
            raise TypeError(f"Unsupported physical union: {annotation}")
        annotation = remaining[0]
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if get_origin(annotation) is Literal:
        if not all(isinstance(value, str) for value in get_args(annotation)):
            raise TypeError("Physical literal values must be strings")
        annotation = str
    scalar = {
        str: "string",
        int: "int64",
        bool: "bool",
        Decimal: "decimal128",
        datetime: "timestamp",
        date: "date",
    }
    if annotation in scalar:
        return FactField(name, scalar[annotation], nullable)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return FactField(name, "struct", nullable, _fields(annotation))
    raise TypeError(f"Unsupported physical field: {annotation}")


def _fields(model: type[BaseModel]) -> tuple[FactField, ...]:
    return tuple(_field(name, field.annotation) for name, field in model.model_fields.items())


@lru_cache(maxsize=len(RECORD_MODELS))
def fact_fields(record_type: RecordType) -> tuple[FactField, ...]:
    return _fields(RECORD_MODELS[record_type])
