from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PINNED_PNPM = Path(
    "/home/alwin/apps/tickflow-recovery-artifacts/20260724-1605/"
    "pnpm-runtime/node_modules/pnpm/bin/pnpm.cjs"
)


def test_frontend_realtime_client_suite() -> None:
    preview = (ROOT / "frontend/src/components/StockPreviewDialog.tsx").read_text(
        encoding="utf-8"
    )
    dow_monitor = (ROOT / "frontend/src/pages/DowMonitor.tsx").read_text(
        encoding="utf-8"
    )
    quote_stream = (ROOT / "frontend/src/lib/useQuoteStream.ts").read_text(
        encoding="utf-8"
    )
    assert "useRealtimeMarketData" in preview
    assert "useRealtimeMarketData" in dow_monitor
    assert "/api/intraday/stream" in quote_stream
    pnpm = shutil.which("pnpm")
    command = [pnpm] if pnpm is not None else ["node", str(PINNED_PNPM)]
    subprocess.run(
        [
            *command,
            "--dir",
            "frontend",
            "exec",
            "vitest",
            "run",
            "src/lib/realtimeMarketData.test.ts",
            "src/lib/realtimeOverlays.test.ts",
            "src/lib/intraday-market.test.ts",
            "src/pages/DowMonitor.test.tsx",
            "src/components/dow-monitor/DowMonitorDetailDialog.test.tsx",
        ],
        cwd=ROOT,
        check=True,
    )
