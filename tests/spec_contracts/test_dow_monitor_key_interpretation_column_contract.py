from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SPECIFICATION_ID = "USER-20260730-DOW-MONITOR-KEY-INTERPRETATION-COLUMN"
REQUIREMENT_ID = "REQ-DOW-MONITOR-KEY-INTERPRETATION-COLUMN-001"
BEHAVIOR_TESTS = (
    "src/components/dow-monitor/keyInterpretation.test.ts",
    "src/components/dow-monitor/DowMonitorList.test.tsx",
    "src/pages/DowMonitorHelp.test.tsx",
)


def test_key_interpretation_requirement_is_authoritative_and_traceable() -> None:
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
        "path": (
            "tests/spec_contracts/"
            "test_dow_monitor_key_interpretation_column_contract.py"
        ),
        "type": "executable-test",
    }
    for evidence in entry["acceptance"]:
        assert (ROOT / evidence["path"]).is_file()


def test_key_interpretation_behavioral_suite() -> None:
    frontend = ROOT / "frontend"
    missing = [path for path in BEHAVIOR_TESTS if not (frontend / path).is_file()]
    if missing:
        pytest.skip(f"behavioral implementation has not created: {missing}")

    pnpm = shutil.which("pnpm")
    assert pnpm is not None
    subprocess.run(
        [pnpm, "--dir", "frontend", "exec", "vitest", "run", *BEHAVIOR_TESTS],
        cwd=ROOT,
        check=True,
    )
