import json
import subprocess
import sys

import pytest
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.data import EventQuery, OpeningObservation
from tivelo_contracts.errors import TiveloError
from tivelo_contracts.protocols import ExecutionDataReader
from tivelo_contracts.testing import InMemoryReader


def opening(**changes):
    values = dict(
        instrument_id="security-1",
        event_at="2025-01-02T01:30:00Z",
        trading_date="2025-01-02",
        session_id="fixture.open",
        state="tradable",
        raw_open="10.01",
        quote_currency="CNY",
        reference_price="10",
        reference_kind="previous_close",
        available_at="2025-01-02T01:25:00Z",
        ingested_at="2025-01-02T01:25:01Z",
        source_id="fixture",
        revision_id="1",
    )
    values.update(changes)
    return OpeningObservation(**values)


def query(as_of):
    return EventQuery(
        instrument_ids=("security-1",),
        start="2025-01-02T00:00:00Z",
        end="2025-01-03T00:00:00Z",
        as_of=as_of,
    )


def test_snapshot_opening_revisions_match_independent_reader(make_engine):
    records = (
        opening(),
        opening(
            raw_open="10.02",
            revision_id="2",
            available_at="2025-01-02T07:00:00Z",
            ingested_at="2025-01-02T07:00:00Z",
        ),
    )
    memory = InMemoryReader(openings=records, knowledge_basis="historical_versions")
    engine, _, plan = make_engine(records, record_type="opening_observation")
    snapshot = engine.create_snapshot((engine.publish(engine.sync(plan)).version_id,))
    with engine.open_reader(snapshot.snapshot_id) as reader:
        assert isinstance(reader, ExecutionDataReader)
        for moment in ("2025-01-02T01:29:59Z", "2025-01-02T01:30:00Z", "2025-01-02T07:00:00Z"):
            assert reader.openings(query(moment)) == memory.openings(query(moment))


@pytest.mark.parametrize(
    "record",
    [opening(is_final=False), opening(event_at="2024-01-02T01:30:00Z", trading_date="2024-01-02")],
)
def test_ingestion_rejects_unfinished_or_out_of_scope_openings(make_engine, record):
    engine, _, plan = make_engine((record,), record_type="opening_observation")
    with pytest.raises(TiveloError):
        engine.sync(plan)


def test_cli_import_and_query_opening_fact(make_engine, tmp_path):
    observation = opening(state="suspended", raw_open=None)
    _, _, plan = make_engine((observation,), record_type="opening_observation")
    request, source, query_path = (
        tmp_path / name for name in ("request.json", "opening.jsonl", "query.json")
    )
    request.write_bytes(canonical_bytes(plan.request))
    source.write_bytes(canonical_bytes(observation) + b"\n")
    query_path.write_bytes(canonical_bytes(query("2025-01-02T01:30:00Z")))
    prefix = [sys.executable, "-m", "tivelo_data.cli", "--root", str(tmp_path / "cli-store")]
    imported = subprocess.run(
        [
            *prefix,
            "import-jsonl",
            "--request",
            str(request),
            "--input",
            str(source),
            "--source-id",
            "fixture",
            "--knowledge-basis",
            "historical_versions",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    snapshot = json.loads(imported.stdout)["snapshot_id"]
    result = subprocess.run(
        [
            *prefix,
            "query",
            "--snapshot",
            snapshot,
            "--request",
            str(query_path),
            "--record-type",
            "opening_observation",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert OpeningObservation.model_validate_json(result.stdout) == observation
