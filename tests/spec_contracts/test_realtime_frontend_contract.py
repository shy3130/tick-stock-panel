from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PNPM = Path(
    "/home/alwin/apps/tickflow-recovery-artifacts/20260724-1605/"
    "pnpm-runtime/node_modules/pnpm/bin/pnpm.cjs"
)


def test_frontend_realtime_client_suite() -> None:
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
            "src/lib/intraday-market.test.ts",
        ],
        cwd=ROOT,
        check=True,
    )
