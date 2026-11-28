"""Immutable v2 wire types; no application state or storage dependencies.

Content hashes use Tivelo's Python JSON encoding v1. This is deliberately not
RFC 8785 and must not be reused for commercial license signatures.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationInfo,
    WithJsonSchema,
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


Key = Annotated[str, Field(strict=True, min_length=1, max_length=256, pattern=r"^\S(?:.*\S)?$")]
ReleaseId = Annotated[
    str, Field(strict=True, max_length=128, pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Currency = Annotated[str, Field(strict=True, pattern=r"^[A-Z]{3}$")]


def utc_datetime(value: Any) -> datetime:
    if isinstance(value, str):
        if (
            re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})",
                value,
            )
            is None
        ):
            raise ValueError("expected ISO 8601 timestamp with timezone and microsecond precision")
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("expected ISO 8601 timestamp with timezone") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("v2 timestamps must include a timezone; v1 requires an explicit adapter")
    try:
        return value.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise ValueError("timestamp cannot be represented in UTC") from exc


UTCDateTime = Annotated[datetime, BeforeValidator(utc_datetime)]


def calendar_date(value: Any) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return date.fromisoformat(value)
    raise ValueError("calendar dates must be dates or YYYY-MM-DD strings, never timestamps")


CalendarDate = Annotated[date, BeforeValidator(calendar_date)]


def decimal_text(value: Decimal) -> str:
    if not value:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


DECIMAL_PATTERN = r"^-?(?:0|[1-9][0-9]{0,25})(?:\.[0-9]{1,12})?$"
NONNEGATIVE_PATTERN = r"^(?:0|[1-9][0-9]{0,25})(?:\.[0-9]{1,12})?$"


def exact_decimal(value: Any, info: ValidationInfo) -> Decimal:
    if info.mode == "json" and (
        not isinstance(value, str) or re.fullmatch(DECIMAL_PATTERN, value) is None
    ):
        raise ValueError("wire decimals must be fixed-point decimal128 strings")
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("use a decimal string, Decimal or integer; binary floats are not facts")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("non-finite financial values are not allowed")
    if result:
        _, digits, exponent = result.as_tuple()
        trailing_zeroes = 0
        for digit in reversed(digits):
            if digit:
                break
            trailing_zeroes += 1
        if result.adjusted() >= 26 or -(exponent + trailing_zeroes) > 12:
            raise ValueError("value does not fit decimal128(38,12) without rounding")
    # decimal128(38,12): at most 26 integral and 12 fractional places. Do not
    # use Decimal.normalize/quantize here: their default context can round.
    canonical = decimal_text(result)
    integral, _, fractional = canonical.lstrip("-").partition(".")
    if len(integral.lstrip("0")) > 26 or len(fractional) > 12:
        raise ValueError("value does not fit decimal128(38,12) without rounding")
    return Decimal(canonical)


ExactDecimal = Annotated[
    Decimal,
    BeforeValidator(exact_decimal),
    PlainSerializer(decimal_text, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "decimal128-38-12", "pattern": DECIMAL_PATTERN}),
]
NonNegativeDecimal = Annotated[
    ExactDecimal,
    Field(ge=0),
    WithJsonSchema(
        {"type": "string", "format": "decimal128-38-12", "pattern": NONNEGATIVE_PATTERN}
    ),
]
PositiveDecimal = Annotated[
    ExactDecimal,
    Field(gt=0),
    WithJsonSchema(
        {
            "type": "string",
            "format": "decimal128-38-12",
            "pattern": NONNEGATIVE_PATTERN,
            "not": {"pattern": r"^0(?:\.0+)?$"},
        }
    ),
]
UnitRatio = Annotated[
    ExactDecimal,
    Field(ge=0, le=1),
    WithJsonSchema(
        {
            "type": "string",
            "format": "decimal128-38-12",
            "pattern": r"^(?:0(?:\.[0-9]{1,12})?|1(?:\.0{1,12})?)$",
        }
    ),
]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def parse_json(text: str) -> Any:
    if len(text.encode("utf-8")) > 1_048_576:
        raise ValueError("contract JSON exceeds 1 MiB")
    try:
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (RecursionError, OverflowError) as exc:
        raise ValueError("contract JSON exceeds parser limits") from exc


def canonical_json(text: str) -> str:
    value = parse_json(text)
    if not isinstance(value, dict):
        raise ValueError("parameters must be a JSON object")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


ParametersJSON = Annotated[str, Field(strict=True), AfterValidator(canonical_json)]


def canonical_bytes(value: Contract) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def fingerprint(value: Contract) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise ValueError("artifact path must be a normalized relative POSIX path")
    return value


RelativePath = Annotated[Key, AfterValidator(relative_path)]
