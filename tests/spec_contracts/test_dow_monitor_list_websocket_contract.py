from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


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
