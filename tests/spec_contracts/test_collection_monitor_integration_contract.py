from __future__ import annotations

from pathlib import Path

import yaml


REPOSITORY = Path(__file__).resolve().parents[2]
REQUIREMENT_IDS = {
    "REQ-COLLECTION-MONITOR-PROXY-001",
    "REQ-COLLECTION-MONITOR-PAGE-001",
    "REQ-COLLECTION-MONITOR-PREACCEPTANCE-001",
}
CONTRACT_RECORDS = (
    "docs/specs/collection-monitor-integration.md",
    "docs/spec-index.yaml",
    "docs/traceability.yaml",
    "docs/acceptance/collection-monitor-integration.md",
    "docs/reviews/collection-monitor-integration.md",
)
PAGE_BEHAVIORAL_TEST = (
    "tests/spec_contracts/test_collection_monitor_frontend_behavior.py"
)


def _read(relative_path: str) -> str:
    return (REPOSITORY / relative_path).read_text(encoding="utf-8")


def test_all_requirements_appear_in_every_contract_record() -> None:
    for relative_path in CONTRACT_RECORDS:
        contents = _read(relative_path)
        missing = {
            requirement_id
            for requirement_id in REQUIREMENT_IDS
            if requirement_id not in contents
        }
        assert not missing, f"{relative_path} is missing {sorted(missing)}"


def test_collection_monitor_traceability_paths_exist() -> None:
    traceability = yaml.safe_load(_read("docs/traceability.yaml"))
    requirements = {
        entry["id"]: entry
        for entry in traceability["requirements"]
        if entry["id"] in REQUIREMENT_IDS
    }

    assert set(requirements) == REQUIREMENT_IDS
    for requirement_id, requirement in requirements.items():
        implementation_paths = requirement["implementation"]
        if isinstance(implementation_paths, str):
            implementation_paths = [implementation_paths]
        for relative_path in implementation_paths:
            assert (REPOSITORY / relative_path).is_file(), (
                f"{requirement_id} has missing implementation evidence: {relative_path}"
            )

        test_evidence = requirement["tests"]
        if isinstance(test_evidence, dict):
            test_evidence = [test_evidence]
        for evidence in test_evidence:
            relative_path = Path(evidence["path"])
            assert evidence["type"] == "executable-test"
            assert relative_path.parts[0] == "tests", (
                f"{requirement_id} test evidence must be rooted under tests/: "
                f"{relative_path.as_posix()}"
            )
            assert (REPOSITORY / relative_path).is_file(), (
                f"{requirement_id} has missing test evidence: {relative_path.as_posix()}"
            )


def test_page_and_preacceptance_requirements_map_to_frontend_behavioral_wrapper() -> None:
    traceability = yaml.safe_load(_read("docs/traceability.yaml"))
    requirements = {
        entry["id"]: entry for entry in traceability["requirements"]
    }

    for requirement_id in (
        "REQ-COLLECTION-MONITOR-PAGE-001",
        "REQ-COLLECTION-MONITOR-PREACCEPTANCE-001",
    ):
        test_evidence = requirements[requirement_id]["tests"]
        if isinstance(test_evidence, dict):
            test_evidence = [test_evidence]

        assert PAGE_BEHAVIORAL_TEST in {
            evidence["path"] for evidence in test_evidence
        }, f"{requirement_id} must map to {PAGE_BEHAVIORAL_TEST}"


def test_live_semantic_acceptance_remains_pending() -> None:
    acceptance = _read("docs/acceptance/collection-monitor-integration.md")

    assert "Status: pending live semantic acceptance" in acceptance
    assert "Live semantic evidence: pending." in acceptance
