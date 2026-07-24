from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_gateway_behavioral_suite() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests/test_realtime_market_data.py",
            "backend/tests/test_realtime_websocket.py",
            "-q",
        ],
        cwd=ROOT,
        check=True,
    )
