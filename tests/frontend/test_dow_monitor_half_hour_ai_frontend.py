from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


def test_half_hour_ai_frontend_contract() -> None:
    node = shutil.which("node")
    assert node is not None
    completed = subprocess.run(
        [
            node,
            str(FRONTEND / "node_modules" / "vitest" / "vitest.mjs"),
            "run",
            "src/components/dow-monitor/DowMonitorHalfHourAiButton.test.tsx",
            "src/components/dow-monitor/formatServerTimestamp.test.ts",
            "src/components/dow-monitor/DowMonitorList.test.tsx",
        ],
        cwd=FRONTEND,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
