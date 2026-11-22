"""One explicit physical representation of the v2 fact contracts."""

from __future__ import annotations

from functools import lru_cache

import pyarrow as pa
from tivelo_contracts.base import Contract
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.facts import BUSINESS_KEYS as BUSINESS_KEYS
from tivelo_contracts.facts import RECORD_MODELS, FactField, fact_fields
from tivelo_contracts.facts import REVISION_KEYS as REVISION_KEYS


def model_for(record_type: str) -> type[Contract]:
    try:
        return RECORD_MODELS[record_type]
    except KeyError as exc:
        raise TiveloError(ErrorCode.SCHEMA_UNSUPPORTED, "Unknown record type") from exc


def _arrow_field(field: FactField) -> pa.Field:
    scalar = {
        "string": pa.string(),
        "int64": pa.int64(),
        "bool": pa.bool_(),
        "decimal128": pa.decimal128(38, 12),
        "timestamp": pa.timestamp("us", tz="UTC"),
        "date": pa.date32(),
    }
    dtype = (
        pa.struct([_arrow_field(child) for child in field.children])
        if field.kind == "struct"
        else scalar[field.kind]
    )
    return pa.field(field.name, dtype, nullable=field.nullable)


@lru_cache(maxsize=len(RECORD_MODELS))
def arrow_schema(record_type: str) -> pa.Schema:
    return pa.schema(
        [_arrow_field(field) for field in fact_fields(record_type)],
        metadata={
            b"tivelo.schema": b"2.0",
            b"tivelo.record_type": record_type.encode(),
            b"tivelo.timezone": b"UTC",
            b"tivelo.decimal": b"decimal128(38,12)",
        },
    )


def record_table(record_type: str, records: tuple[Contract, ...]) -> pa.Table:
    model = model_for(record_type)
    # Revalidate models as dictionaries too: model_copy/model_construct can bypass
    # validation, so accepting an instance alone is not an ingestion boundary.
    values = [
        model.model_validate(r.model_dump(mode="python")).model_dump(mode="python") for r in records
    ]
    return pa.Table.from_pylist(values, schema=arrow_schema(record_type))
