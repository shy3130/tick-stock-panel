from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_dow_monitor_presentation_behavioral_suite() -> None:
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
            "src/components/EChartsCandlestick.test.tsx",
            "src/components/dow-monitor/DowMonitorDetailDialog.test.tsx",
        ],
        cwd=ROOT,
        check=True,
    )
