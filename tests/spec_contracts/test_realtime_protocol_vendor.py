from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "realtime-ui"
SCHEMA = CONTRACT / "v1.schema.json"
CHECKSUM = CONTRACT / "v1.sha256"


def _load(name: str) -> dict:
    return json.loads((CONTRACT / "fixtures" / name).read_text(encoding="utf-8"))


def test_vendored_protocol_checksum() -> None:
    expected = CHECKSUM.read_text(encoding="utf-8").strip()
    actual = hashlib.sha256(SCHEMA.read_bytes()).hexdigest()
    assert actual == expected


def test_shared_fixtures_have_same_validity() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(_load("update-valid.json"), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_load("update-invalid-account.json"), schema)
