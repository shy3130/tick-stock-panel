from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PNPM = Path(
    "/home/alwin/apps/tickflow-recovery-artifacts/20260724-1605/"
    "pnpm-runtime/node_modules/pnpm/bin/pnpm.cjs"
)


def test_frontend_realtime_client_suite() -> None:
    preview = (ROOT / "frontend/src/components/StockPreviewDialog.tsx").read_text()
    dow_monitor = (ROOT / "frontend/src/pages/DowMonitor.tsx").read_text()
    quote_stream = (ROOT / "frontend/src/lib/useQuoteStream.ts").read_text()
    assert "useRealtimeMarketData" in preview
    assert "useRealtimeMarketData" in dow_monitor
    assert "/api/intraday/stream" in quote_stream
    subprocess.run(
        [
            "node",
            str(PNPM),
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
