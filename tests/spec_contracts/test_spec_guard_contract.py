from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def test_repository_specification_contract_passes() -> None:
    repository = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "scripts/check_spec_compliance.py"],
        cwd=repository,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
