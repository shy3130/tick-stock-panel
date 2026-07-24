from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UV = Path("/home/alwin/apps/tickflow-recovery-artifacts/20260724-1435/uv")
PNPM = Path(
    "/home/alwin/apps/tickflow-recovery-artifacts/20260724-1435/"
    "pnpm-runtime/node_modules/pnpm/bin/pnpm.cjs"
)


def test_recovered_backend_characterization() -> None:
    subprocess.run(
        [
            str(UV),
            "run",
            "--project",
            "backend",
            "--extra",
            "dev",
            "pytest",
            "backend/tests/test_intraday_index_quotes.py",
            "backend/tests/test_stock_detail_realtime_candle.py",
            "backend/tests/test_strategy_realtime_refresh.py",
            "backend/tests/test_dow_strategy_proxy.py",
            "-q",
        ],
        cwd=ROOT,
        check=True,
    )


def test_recovered_frontend_characterization() -> None:
    subprocess.run(
        [
            "node",
            str(PNPM),
            "--dir",
            "frontend",
            "exec",
            "vitest",
            "run",
            "src/pages/dow-monitor-route.test.tsx",
            "src/pages/DowMonitor.test.tsx",
            "src/components/dow-monitor/DowMonitorDetailDialog.test.tsx",
            "src/pages/Screener.dow-strategy.test.tsx",
            "src/components/screener/DowStrategyCard.test.tsx",
        ],
        cwd=ROOT,
        check=True,
    )
