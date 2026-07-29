from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]

GROUP_REQUIREMENTS = {
    "REQ-DOW-MONITOR-INDICATOR-GROUPS-LAYOUT-001",
    "REQ-DOW-MONITOR-LIVE-OBSERVATION-METRICS-001",
    "REQ-DOW-MONITOR-STABLE-DECISION-METRICS-001",
    "REQ-DOW-MONITOR-INDICATOR-SIGNAL-BOUNDARY-001",
}


def test_grouped_indicator_requirements_are_authoritative_and_traceable() -> None:
    index = yaml.safe_load((ROOT / "docs/spec-index.yaml").read_text(encoding="utf-8"))
    traceability = yaml.safe_load(
        (ROOT / "docs/traceability.yaml").read_text(encoding="utf-8")
    )
    specification = next(
        item
        for item in index["specifications"]
        if item["id"] == "USER-20260729-DOW-MONITOR-LIST-WEBSOCKET"
    )
    assert GROUP_REQUIREMENTS <= set(specification["requirements"])

    entries = {
        item["id"]: item
        for item in traceability["requirements"]
        if item["id"] in GROUP_REQUIREMENTS
    }
    assert set(entries) == GROUP_REQUIREMENTS
    for requirement_id, entry in entries.items():
        assert entry["specification"] == specification["id"]
        assert entry["implementation"]
        assert entry["tests"]
        assert entry["acceptance"]
        for evidence in entry["acceptance"]:
            assert (ROOT / evidence["path"]).is_file(), requirement_id


def test_dow_monitor_list_websocket_behavioral_suite() -> None:
    """Breaks when list semantics or the realtime/decision boundary regresses."""
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
            "src/components/dow-monitor/monitorListPresentation.test.ts",
            "src/components/dow-monitor/DowMonitorList.test.tsx",
            "src/components/dow-monitor/DowMonitorDetailPanel.test.tsx",
            "src/pages/DowMonitor.test.tsx",
            "src/lib/realtimeMarketData.test.ts",
        ],
        cwd=ROOT,
        check=True,
    )
