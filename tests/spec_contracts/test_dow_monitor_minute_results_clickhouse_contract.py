from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = "docs/specs/dow-monitor-minute-results-clickhouse.md"
SPEC = ROOT / SPEC_PATH
SPECIFICATION = "USER-20260729-DOW-MONITOR-MINUTE-RESULTS-CLICKHOUSE"
REQUIREMENTS = {
    "REQ-DOW-MONITOR-MINUTE-RESULTS-SCHEMA-001",
    "REQ-DOW-MONITOR-MINUTE-RESULTS-MATERIALIZATION-001",
    "REQ-DOW-MONITOR-MINUTE-RESULTS-BACKFILL-001",
}


def test_minute_result_requirements_are_authoritative_and_traceable() -> None:
    index = yaml.safe_load((ROOT / "docs/spec-index.yaml").read_text(encoding="utf-8"))
    trace = yaml.safe_load((ROOT / "docs/traceability.yaml").read_text(encoding="utf-8"))
    indexed_specs = [
        item
        for item in index["specifications"]
        if item["id"] == SPECIFICATION
    ]
    assert len(indexed_specs) == 1
    indexed = indexed_specs[0]
    assert indexed["path"] == SPEC_PATH
    assert indexed["status"] == "authoritative"
    assert set(indexed["requirements"]) == REQUIREMENTS

    traced = {
        item["id"]: item
        for item in trace["requirements"]
        if item["id"] in REQUIREMENTS
    }
    assert SPEC.is_file()
    assert set(traced) == REQUIREMENTS

    text = SPEC.read_text(encoding="utf-8")
    for requirement_id, item in traced.items():
        assert f"## {requirement_id}" in text
        assert item["specification"] == SPECIFICATION
        assert item["implementation"]
        assert item["tests"]
        assert all(
            test["type"] == "executable-test"
            and test["path"].startswith("tests/")
            for test in item["tests"]
        )
        assert {entry["type"] for entry in item["acceptance"]} == {
            "semantic-acceptance",
            "independent-review",
        }
