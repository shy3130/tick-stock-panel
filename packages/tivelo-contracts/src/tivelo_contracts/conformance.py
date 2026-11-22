"""Public, offline test vectors for implementations of DataReader and ExecutionDataReader."""

import json
from importlib.resources import files

from .base import Contract, canonical_bytes
from .data import BarQuery, DatasetVersion, EventQuery, HistoryQuery, SnapshotManifest
from .facts import RECORD_MODELS
from .protocols import DataReader, ExecutionDataReader

_GROUPS = {
    "bars": ("bar", BarQuery),
    "instruments": ("instrument", HistoryQuery),
    "actions": ("corporate_action", HistoryQuery),
    "factors": ("adjustment_factor", HistoryQuery),
    "openings": ("opening_observation", EventQuery),
}


def _vectors():
    resource = files("tivelo_contracts").joinpath("fixtures", "reader-v2.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def _records(vectors):
    return {
        method: {
            name: RECORD_MODELS[kind].model_validate(value)
            for name, value in vectors["records"][method].items()
        }
        for method, (kind, _) in _GROUPS.items()
    }


def reader_fixture() -> dict[str, tuple[Contract, ...]]:
    """Return fresh public facts for loading into an isolated historical-versions snapshot."""
    return {method: tuple(records.values()) for method, records in _records(_vectors()).items()}


def verify_reader(reader: DataReader, *, execution: bool = False) -> tuple[str, ...]:
    """Check the published queries and their expected full records. Raise on the first difference."""
    if not isinstance(reader, ExecutionDataReader if execution else DataReader):
        raise AssertionError("Reader is missing a required public operation")
    manifest = SnapshotManifest.model_validate_json(canonical_bytes(reader.manifest))
    versions = tuple(
        DatasetVersion.model_validate_json(canonical_bytes(item)) for item in reader.datasets
    )
    declared = [(item.dataset_id, item.version_id) for item in manifest.contents.datasets]
    if declared != sorted((item.contents.dataset_id, item.version_id) for item in versions):
        raise AssertionError("The snapshot and reader expose different dataset versions")
    vectors = _vectors()
    required = {kind for group, (kind, _) in _GROUPS.items() if execution or group != "openings"}
    present = {
        item.contents.record_type
        for item in versions
        if item.contents.knowledge_basis == vectors["knowledge_basis"]
        and item.contents.coverage.status == "complete"
    }
    if not required <= present:
        raise AssertionError("Load the complete historical fixture for every tested record type")
    records = _records(vectors)
    checked = []
    for case in vectors["cases"]:
        method = case["method"]
        if method == "openings" and not execution:
            continue
        kind, query_type = _GROUPS[method]
        actual = getattr(reader, method)(query_type.model_validate(case["query"]))
        if not isinstance(actual, tuple) or not all(
            isinstance(row, RECORD_MODELS[kind]) for row in actual
        ):
            raise AssertionError(f"{case['id']}: reader returned an invalid result type")
        expected = [records[method][name] for name in case["expected"]]
        # Across independent instruments the protocol does not prescribe physical row order.
        if sorted(map(canonical_bytes, actual)) != sorted(map(canonical_bytes, expected)):
            raise AssertionError(f"{case['id']}: returned facts differ from the published fixture")
        checked.append(case["id"])
    return tuple(checked)
