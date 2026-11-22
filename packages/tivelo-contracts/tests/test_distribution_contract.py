import ast
import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import TypeAdapter, ValidationError

import tivelo_contracts
from tivelo_contracts.base import (
    NonNegativeDecimal,
    PositiveDecimal,
    UnitRatio,
    canonical_bytes,
    fingerprint,
)
from tivelo_contracts.data import DatasetScope
from tivelo_contracts.schemas import schema_bundle


def test_public_package_cannot_import_application_or_engine_implementations():
    forbidden = {"app", "tivelo_data", "tivelo_backtest", "tivelo_market_cn", "fastapi", "duckdb"}
    for file in Path(tivelo_contracts.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                assert node.module.split(".")[0] not in forbidden, file.name
            elif isinstance(node, ast.Import):
                assert not {alias.name.split(".")[0] for alias in node.names} & forbidden, file.name


def test_schema_exposes_closed_records_and_decimal_string_wire_format():
    schema = schema_bundle()
    assert schema["$schema"].endswith("2020-12/schema")
    for name in (
        "Bar",
        "Instrument",
        "CorporateAction",
        "BacktestSpec",
        "RunLock",
        "OrderBookObservation",
        "TradePrint",
    ):
        assert schema["$defs"][name]["additionalProperties"] is False
    assert schema["$defs"]["Bar"]["properties"]["volume"]["type"] == "string"
    assert schema["$defs"]["Bar"]["properties"]["available_at"]["format"] == "date-time"
    book = schema["$defs"]["OrderBookObservation"]
    trade = schema["$defs"]["TradePrint"]
    assert trade["properties"]["quantity"]["type"] == "string"
    assert trade["properties"]["sequence"]["type"] == "integer"
    assert {"best_ask", "best_bid", "last_trade"} <= set(book["required"])
    assert book["properties"]["price_basis"]["const"] == "raw"


def test_schema_and_model_agree_on_a_valid_bar(make_bar):
    schema = schema_bundle()
    schema["$ref"] = "#/$defs/Bar"
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        json.loads(canonical_bytes(make_bar()))
    )


def test_data_coverage_evidence_and_bar_convention_are_part_of_identity():
    values = dict(
        market_id="cn.equity",
        asset_class="equity",
        instrument_ids=("security-1",),
        start="2025-01-01T00:00:00Z",
        end="2025-02-01T00:00:00Z",
        timeframes=("1d",),
        bar_convention_id="example.daily",
    )
    received = DatasetScope(**values)
    validated = DatasetScope(**values, coverage_basis="validated_market_sessions")
    different_clock = DatasetScope(**{**values, "bar_convention_id": "example.daily-other"})
    assert received.coverage_basis == "source_response"
    assert len({fingerprint(scope) for scope in (received, validated, different_clock)}) == 3
    schema = schema_bundle()
    schema["$ref"] = "#/$defs/DatasetScope"
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        json.loads(canonical_bytes(received))
    )


@pytest.mark.parametrize(
    "kind,value",
    [
        (NonNegativeDecimal, "-1"),
        (PositiveDecimal, "0"),
        (PositiveDecimal, "0.000"),
        (UnitRatio, "1.01"),
        (UnitRatio, "-0.1"),
        (NonNegativeDecimal, "1e3"),
        (NonNegativeDecimal, 100),
        (NonNegativeDecimal, "0.0000000000001"),
    ],
)
def test_schema_and_runtime_both_reject_invalid_wire_decimals(kind, value):
    adapter = TypeAdapter(kind)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(value, adapter.json_schema())
    with pytest.raises(ValidationError):
        adapter.validate_json(json.dumps(value))
