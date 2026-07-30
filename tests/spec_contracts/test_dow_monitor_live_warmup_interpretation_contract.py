from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPECIFICATION_ID = "USER-20260730-DOW-MONITOR-LIVE-WARMUP-INTERPRETATION"
REQUIREMENT_ID = "REQ-DOW-MONITOR-LIVE-WARMUP-INTERPRETATION-001"
BEHAVIOR_TESTS = (
    "src/components/dow-monitor/interpretationMarketContext.test.ts",
    "src/components/dow-monitor/keyInterpretation.test.ts",
    "src/components/dow-monitor/monitorListPresentation.test.ts",
    "src/pages/DowMonitor.test.tsx",
)


def test_live_warmup_requirement_is_authoritative_and_traceable() -> None:
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
    for implementation in entry["implementation"]:
        assert (ROOT / implementation).is_file()
    assert entry["tests"] == [{
        "path": (
            "tests/spec_contracts/"
            "test_dow_monitor_live_warmup_interpretation_contract.py"
        ),
        "type": "executable-test",
    }]
    for evidence in entry["acceptance"]:
        assert (ROOT / evidence["path"]).is_file()


def test_live_warmup_behavioral_suite() -> None:
    frontend = ROOT / "frontend"
    missing = [path for path in BEHAVIOR_TESTS if not (frontend / path).is_file()]
    assert not missing, f"missing behavioral evidence: {missing}"

    pnpm = shutil.which("pnpm")
    assert pnpm is not None
    subprocess.run(
        [pnpm, "--dir", "frontend", "exec", "vitest", "run", *BEHAVIOR_TESTS],
        cwd=ROOT,
        check=True,
    )
