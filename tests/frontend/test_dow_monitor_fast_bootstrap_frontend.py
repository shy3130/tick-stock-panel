from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


def test_fast_bootstrap_frontend_contracts() -> None:
    node = shutil.which("node")
    assert node is not None, "node executable is required for frontend contract tests"
    completed = subprocess.run(
        [
            node,
            str(FRONTEND / "node_modules" / "vitest" / "vitest.mjs"),
            "run",
            "src/components/dow-monitor/useDowMonitor.test.tsx",
            "src/components/dow-monitor/monitorListPresentation.test.ts",
            "src/pages/DowMonitor.test.tsx",
        ],
        cwd=FRONTEND,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
