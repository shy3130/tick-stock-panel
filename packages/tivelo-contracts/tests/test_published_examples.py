import json
from importlib.resources import files
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from tivelo_contracts.data import Bar, Coverage, DatasetVersion, SnapshotManifest
from tivelo_contracts.facts import RECORD_MODELS
from tivelo_contracts.schemas import schema_bundle

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_published_valid_and_invalid_bar_examples():
    raw = (EXAMPLES / "bar.raw.json").read_text(encoding="utf-8")
    assert Bar.model_validate_json(raw).price_basis == "raw"
    for case in json.loads((EXAMPLES / "bar.invalid-overrides.json").read_text(encoding="utf-8")):
        data = {**json.loads(raw), **case["replace"]}
        with pytest.raises(ValidationError):
            Bar.model_validate_json(json.dumps(data))


def test_shipped_wire_vectors_match_the_schema_and_runtime():
    root = files("tivelo_contracts").joinpath("fixtures")
    vectors = json.loads(root.joinpath("reader-v2.json").read_text(encoding="utf-8"))
    document = json.loads(root.joinpath("versioned-facts-v2.json").read_text(encoding="utf-8"))
    schema = schema_bundle()
    types = {
        m.__name__: m for m in (*RECORD_MODELS.values(), Coverage, DatasetVersion, SnapshotManifest)
    }
    facts = {
        "bars": "Bar",
        "instruments": "Instrument",
        "actions": "CorporateAction",
        "factors": "AdjustmentFactor",
        "openings": "OpeningObservation",
    }
    valid = [
        (facts[kind], row) for kind, rows in vectors["records"].items() for row in rows.values()
    ]
    valid.extend((example["model"], example["value"]) for example in document["valid"].values())
    for model, row in valid:
        validator = jsonschema.Draft202012Validator(
            {**schema, "$ref": "#/$defs/" + model}, format_checker=jsonschema.FormatChecker()
        )
        validator.validate(row)
        types[model].model_validate(row)
    for example in document["invalid"]:
        base = document["valid"][example["base"]]
        with pytest.raises(ValidationError):
            types[base["model"]].model_validate({**base["value"], **example["replace"]})
    dataset = DatasetVersion.model_validate(document["valid"]["dataset-version"]["value"])
    snapshot = SnapshotManifest.model_validate(document["valid"]["snapshot-manifest"]["value"])
    assert [(r.dataset_id, r.version_id) for r in snapshot.contents.datasets] == [
        (dataset.contents.dataset_id, dataset.version_id)
    ]
