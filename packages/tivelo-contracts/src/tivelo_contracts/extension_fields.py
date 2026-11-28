"""Point-in-time observations from user-defined research tables."""

from typing import Literal, Self

from pydantic import Field, StrictBool, model_validator

from .base import Contract, Digest, ExactDecimal, Key, UTCDateTime, fingerprint
from .errors import ErrorCode, TiveloError


class ExtensionField(Contract):
    table_id: Key
    table_version: Key
    field_id: Key
    field_version: Key
    value_type: Literal["int", "float", "bool", "string"]
    unit: Key
    granularity: Literal["instrument", "market"]
    time_basis: Key = "source_effective_at"


class ExtensionFieldRef(Contract):
    dataset_id: Key
    field_sha256: Digest


class FormulaField(ExtensionFieldRef):
    """A sealed formula input from an immutable, typed snapshot field."""

    alias: str = Field(strict=True, pattern=r"^ext_[a-z][a-z0-9_]{0,59}$")
    value_type: Literal["int", "float", "bool", "string"]
    equals_text: str | None = Field(
        default=None, strict=True, max_length=65_536, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def typed_operand(self) -> Self:
        if (self.value_type == "string") != (self.equals_text is not None):
            raise ValueError("string inputs require an explicit equality predicate")
        return self


def validate_formula_fields(inputs: tuple[FormulaField, ...]) -> None:
    aliases, datasets, fields = set(), {}, {}
    for item in inputs:
        if item.alias in aliases:
            raise ValueError("formula input aliases must be unique")
        aliases.add(item.alias)
        identity = (item.field_sha256, item.value_type)
        if datasets.setdefault(item.dataset_id, identity) != identity or (
            fields.setdefault(item.field_sha256, item.dataset_id) != item.dataset_id
        ):
            raise ValueError("formula inputs disagree on field identity or type")


class ExtensionValue(Contract):
    field_sha256: Digest
    instrument_id: Key | None
    effective_at: UTCDateTime
    valid_until: UTCDateTime | None = None
    available_at: UTCDateTime
    ingested_at: UTCDateTime
    revision_id: Key
    source_id: Key
    int_value: int | None = Field(default=None, strict=True, ge=-(2**63), lt=2**63)
    decimal_value: ExactDecimal | None = None
    bool_value: StrictBool | None = None
    text_value: str | None = Field(default=None, strict=True, max_length=65_536)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.ingested_at < self.available_at:
            raise ValueError("ingestion precedes availability")
        if self.valid_until is not None and self.valid_until <= self.effective_at:
            raise ValueError("extension validity intervals must be nonempty")
        if (
            sum(
                value is not None
                for value in (self.int_value, self.decimal_value, self.bool_value, self.text_value)
            )
            > 1
        ):
            raise ValueError("an extension observation has at most one typed value")
        return self

    def matches(self, field: ExtensionField, digest: str) -> bool:
        return (
            self.field_sha256 == digest
            and (self.instrument_id is None) == (field.granularity == "market")
            and (self.int_value is None or field.value_type == "int")
            and (self.decimal_value is None or field.value_type == "float")
            and (self.bool_value is None or field.value_type == "bool")
            and (self.text_value is None or field.value_type == "string")
        )


class ExtensionQuery(ExtensionFieldRef):
    instrument_ids: tuple[Key, ...]
    effective_at: UTCDateTime
    as_of: UTCDateTime

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.effective_at > self.as_of or len(set(self.instrument_ids)) != len(
            self.instrument_ids
        ):
            raise ValueError("extension queries require a known instant and unique instruments")
        return self


def validate_field_universe(field, instrument_ids):
    if (not instrument_ids) != (field is not None and field.granularity == "market"):
        raise ValueError("only a market-level extension field uses an empty instrument universe")


def resolve_field(datasets, reference, instrument_ids=None):
    version = next((v for v in datasets if v.contents.dataset_id == reference.dataset_id), None)
    if version is None or version.contents.record_type != "extension_value":
        raise TiveloError(
            ErrorCode.UNSUPPORTED_CAPABILITY, "Snapshot lacks the requested extension field"
        )
    scope = version.contents.scope
    if fingerprint(scope.extension_field) != reference.field_sha256:
        raise TiveloError(ErrorCode.DATA_CONFLICT, "Snapshot extension field definition differs")
    if (
        isinstance(reference, ExtensionQuery)
        and not scope.start <= reference.effective_at < scope.end
    ):
        raise TiveloError(
            ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Extension query is outside its declared scope"
        )
    if instrument_ids is None:
        return version
    if scope.extension_field.granularity == "market":
        matches = not instrument_ids
    else:
        matches = bool(instrument_ids) and set(instrument_ids).issubset(scope.instrument_ids)
    if not matches:
        raise TiveloError(
            ErrorCode.DATA_COVERAGE_INSUFFICIENT, "Extension field does not cover this universe"
        )
    return version
