from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPECIFICATION = "SPEC-DOW-MONITOR-OFFLINE-AI-BOOTSTRAP-001"
OLD_SPECIFICATION = "SPEC-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001"
REQUIREMENT_IDS = {
    "REQ-DOW-MONITOR-HALF-HOUR-AI-OFFLINE-BOOTSTRAP-001",
    "REQ-DOW-MONITOR-HALF-HOUR-AI-BOOTSTRAP-ISOLATION-001",
}
SPEC_PATH = "docs/superpowers/specs/2026-07-31-dow-monitor-offline-ai-bootstrap-design.md"
DECISION_PATH = "docs/decisions/2026-07-31-dow-monitor-offline-ai-bootstrap-precedence.md"
STARTUP_RULE = (
    "Startup exception: exactly one latest completed checkpoint before "
    "`created_at` is eligible for bounded offline recovery."
)
NORMAL_RULE = (
    "Normal checkpoint rule: every later completed checkpoint on or after "
    "`created_at` may use bounded offline recovery when canonical minute "
    "results are missing."
)
OLDER_CHECKPOINT_RULE = "Older checkpoints before the eligible startup checkpoint remain prohibited."
INDEX_RESOLUTION = (
    "The bootstrap specification permits exactly one latest completed startup "
    "checkpoint before created_at to use bounded offline recovery; older "
    "checkpoints remain prohibited. Every later normal checkpoint on or after "
    "created_at may also use bounded offline recovery when canonical minute "
    "results are missing."
)


def load_yaml(relative_path: str) -> dict:
    return yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))


def approved_spec_is_authoritative(index: dict) -> bool:
    specifications = [
        entry
        for entry in index["specifications"]
        if entry["id"] == SPECIFICATION
    ]
    return (
        len(specifications) == 1
        and specifications[0]["path"]
        == SPEC_PATH
        and specifications[0]["status"] == "authoritative"
        and set(specifications[0]["requirements"]) == REQUIREMENT_IDS
    )


def old_created_at_conflict_is_resolved(index: dict) -> bool:
    conflicts = [
        entry
        for entry in index["conflicts"]
        if entry["id"] == "CON-20260731-DOW-MONITOR-OFFLINE-AI-BOOTSTRAP-001"
    ]
    return (
        len(conflicts) == 1
        and set(conflicts[0]["specifications"])
        == {OLD_SPECIFICATION, SPECIFICATION}
        and conflicts[0]["status"] == "resolved"
        and conflicts[0]["decision"]
        == DECISION_PATH
        and conflicts[0]["resolution"] == INDEX_RESOLUTION
    )


def traced_requirement_ids(traceability: dict) -> set[str]:
    return {entry["id"] for entry in traceability["requirements"]}


def test_offline_bootstrap_authority_and_traceability_are_registered() -> None:
    index = load_yaml("docs/spec-index.yaml")
    traceability = load_yaml("docs/traceability.yaml")

    assert approved_spec_is_authoritative(index)
    assert old_created_at_conflict_is_resolved(index)
    assert REQUIREMENT_IDS <= traced_requirement_ids(traceability)


def test_precedence_allows_bounded_recovery_for_startup_and_later_normal_checkpoints() -> None:
    design = (ROOT / SPEC_PATH).read_text(encoding="utf-8")
    decision = (ROOT / DECISION_PATH).read_text(encoding="utf-8")

    assert f"Specification ID: `{SPECIFICATION}`" in design
    for text in (design, decision):
        assert STARTUP_RULE in text
        assert NORMAL_RULE in text
        assert OLDER_CHECKPOINT_RULE in text


def test_bootstrap_contract_keeps_websocket_and_realtime_paths_out_of_scope() -> None:
    text = (ROOT / "docs/superpowers/specs/2026-07-31-dow-monitor-offline-ai-bootstrap-design.md").read_text(encoding="utf-8")
    assert "WebSocket" in text
    assert "不改变正式买卖信号" in text
