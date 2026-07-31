from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


def test_live_interpretation_availability_contract() -> None:
    node = shutil.which("node")
    assert node is not None, "node executable is required for frontend contract tests"
    completed = subprocess.run(
        [
            node,
            str(FRONTEND / "node_modules" / "vitest" / "vitest.mjs"),
            "run",
            "src/components/dow-monitor/keyInterpretation.test.ts",
            "src/components/dow-monitor/interpretationMarketContext.test.ts",
        ],
        cwd=FRONTEND,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
