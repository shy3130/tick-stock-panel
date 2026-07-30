from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPECIFICATION_ID = "USER-20260730-DOW-MONITOR-SUDDEN-ANOMALY-HIGHLIGHT"
REQUIREMENT_ID = "REQ-DOW-MONITOR-SUDDEN-ANOMALY-HIGHLIGHT-001"


def test_sudden_anomaly_requirement_is_authoritative_and_traceable() -> None:
    index = yaml.safe_load((ROOT / "docs/spec-index.yaml").read_text(encoding="utf-8"))
    traceability = yaml.safe_load(
        (ROOT / "docs/traceability.yaml").read_text(encoding="utf-8")
    )

    specification = next(
        item
        for item in index["specifications"]
        if item["id"] == SPECIFICATION_ID
    )
    assert specification["status"] == "authoritative"
    assert specification["requirements"] == [REQUIREMENT_ID]

    entry = next(
        item
        for item in traceability["requirements"]
        if item["id"] == REQUIREMENT_ID
    )
    assert entry["specification"] == SPECIFICATION_ID
    assert {
        "frontend/src/components/dow-monitor/DowMonitorList.tsx",
        "frontend/src/pages/DowMonitorHelp.tsx",
    } <= set(entry["implementation"])
    for implementation in entry["implementation"]:
        assert (ROOT / implementation).is_file()
    assert entry["tests"] == {
        "path": "tests/spec_contracts/test_dow_monitor_sudden_anomaly_highlight_contract.py",
        "type": "executable-test",
    }
    for evidence in entry["acceptance"]:
        assert (ROOT / evidence["path"]).is_file()


def test_sudden_anomaly_behavioral_suite() -> None:
    """Breaks when threshold, timer, rendering, or signal-boundary behavior regresses."""
    pnpm = shutil.which("pnpm")
    assert pnpm is not None
    subprocess.run(
        [
            pnpm,
            "--dir",
            "frontend",
            "exec",
            "vitest",
            "run",
            "src/components/dow-monitor/suddenAnomalyHighlights.test.ts",
            "src/components/dow-monitor/useSuddenAnomalyHighlights.test.tsx",
            "src/components/dow-monitor/DowMonitorList.test.tsx",
            "src/pages/DowMonitorHelp.test.tsx",
        ],
        cwd=ROOT,
        check=True,
    )
