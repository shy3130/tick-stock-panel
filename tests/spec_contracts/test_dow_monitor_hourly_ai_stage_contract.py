from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPECIFICATION = "SPEC-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001"
SPEC_PATH = "docs/specs/dow-monitor-half-hour-ai-analysis.md"
DESIGN_PATH = "docs/superpowers/specs/2026-08-01-dow-monitor-hourly-ai-stage-analysis-design.md"
DECISION_FIRST_DESIGN_PATH = (
    "docs/superpowers/specs/"
    "2026-08-02-dow-monitor-hourly-ai-decision-first-view-design.md"
)
DECISION_PATH = "docs/decisions/2026-08-01-dow-monitor-hourly-ai-cadence-precedence.md"
MANUAL_RERUN_DESIGN_PATH = (
    "docs/superpowers/specs/"
    "2026-08-02-dow-monitor-hourly-ai-manual-rerun-design.md"
)
MANUAL_RERUN_DECISION_PATH = (
    "docs/decisions/"
    "2026-08-02-dow-monitor-hourly-ai-manual-rerun-session-exception.md"
)
CONFLICT_ID = "CON-20260801-DOW-MONITOR-HOURLY-AI-CADENCE-001"
REQUIREMENT_IDS = {
    "REQ-DOW-MONITOR-HALF-HOUR-AI-ANALYSIS-001",
    "REQ-DOW-MONITOR-HALF-HOUR-AI-VIEW-001",
    "REQ-DOW-MONITOR-HOURLY-AI-CADENCE-001",
    "REQ-DOW-MONITOR-HOURLY-AI-STAGE-REPORT-001",
    "REQ-DOW-MONITOR-HOURLY-AI-MINUTE-PATH-001",
    "REQ-DOW-MONITOR-HOURLY-AI-VIEW-001",
    "REQ-DOW-MONITOR-HOURLY-AI-DECISION-FIRST-VIEW-001",
    "REQ-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-001",
}


def load_yaml(relative_path: str) -> dict:
    return yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))


def test_hourly_stage_requirements_are_authoritative_and_traced() -> None:
    index = load_yaml("docs/spec-index.yaml")
    traceability = load_yaml("docs/traceability.yaml")

    entries = [item for item in index["specifications"] if item["id"] == SPECIFICATION]
    assert len(entries) == 1
    assert entries[0]["path"] == SPEC_PATH
    assert entries[0]["status"] == "authoritative"
    assert set(entries[0]["requirements"]) == REQUIREMENT_IDS

    traced = {
        item["id"]: item
        for item in traceability["requirements"]
        if item["id"] in REQUIREMENT_IDS
    }
    assert set(traced) == REQUIREMENT_IDS
    for requirement_id in REQUIREMENT_IDS:
        assert traced[requirement_id]["specification"] == SPECIFICATION
        assert traced[requirement_id]["implementation"]
        assert traced[requirement_id]["tests"]
        assert traced[requirement_id]["acceptance"]


def test_hourly_cadence_precedence_is_resolved_without_removing_offline_bootstrap() -> None:
    index = load_yaml("docs/spec-index.yaml")
    conflicts = [item for item in index["conflicts"] if item["id"] == CONFLICT_ID]

    assert len(conflicts) == 1
    assert conflicts[0]["status"] == "resolved"
    assert conflicts[0]["decision"] == DECISION_PATH
    assert set(conflicts[0]["specifications"]) == {
        SPECIFICATION,
        "SPEC-DOW-MONITOR-OFFLINE-AI-BOOTSTRAP-001",
    }

    decision = (ROOT / DECISION_PATH).read_text(encoding="utf-8")
    assert "每小时整点" in decision
    assert "连续交易段收盘" in decision
    assert "只补最新一个" in decision
    assert "离线补足" in decision


def test_authoritative_spec_requires_analysis_instead_of_indicator_narration() -> None:
    spec = (ROOT / SPEC_PATH).read_text(encoding="utf-8")
    design = (ROOT / DESIGN_PATH).read_text(encoding="utf-8")

    for requirement_id in REQUIREMENT_IDS:
        assert requirement_id in spec
    assert "分钟级路径" in spec
    assert "相邻阶段变化" in spec
    assert "当日累计结构" in spec
    assert "持仓者" in spec
    assert "未参与者" in spec
    assert "不得仅复述指标" in spec
    assert "Status: approved" in design


def test_hourly_ai_view_is_decision_first_and_complete_evidence_is_disclosed() -> None:
    spec = (ROOT / SPEC_PATH).read_text(encoding="utf-8")
    design = (ROOT / DECISION_FIRST_DESIGN_PATH).read_text(encoding="utf-8")

    assert "REQ-DOW-MONITOR-HOURLY-AI-DECISION-FIRST-VIEW-001" in spec
    assert "decision summary" in spec
    assert "closed by default" in spec
    assert "strengthening, risk and invalidation" in spec
    assert "Status: approved" in design


def test_manual_hourly_ai_rerun_is_authoritative_and_traced() -> None:
    spec = (ROOT / SPEC_PATH).read_text(encoding="utf-8")
    index = load_yaml("docs/spec-index.yaml")
    traceability = load_yaml("docs/traceability.yaml")
    requirement = "REQ-DOW-MONITOR-HOURLY-AI-MANUAL-RERUN-001"

    assert requirement in spec
    assert (ROOT / MANUAL_RERUN_DESIGN_PATH).is_file()
    assert (ROOT / MANUAL_RERUN_DECISION_PATH).is_file()
    assert requirement in next(
        item["requirements"]
        for item in index["specifications"]
        if item["id"] == SPECIFICATION
    )
    traced = next(
        item
        for item in traceability["requirements"]
        if item["id"] == requirement
    )
    assert traced["specification"] == SPECIFICATION
