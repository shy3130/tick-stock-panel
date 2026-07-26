from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


REPOSITORY = Path(__file__).resolve().parents[2]
RECOVERED_PNPM = Path(
    "/home/alwin/apps/tickflow-recovery-artifacts/20260724-1605/"
    "pnpm-runtime/node_modules/pnpm/bin/pnpm.cjs"
)
FOCUSED_TESTS = (
    "src/pages/CollectionMonitor.test.tsx",
    "src/pages/dow-monitor-route.test.tsx",
)


def _pnpm_command() -> list[str]:
    pnpm = shutil.which("pnpm")
    if pnpm:
        return [pnpm]

    node = shutil.which("node")
    if node and RECOVERED_PNPM.is_file():
        return [node, str(RECOVERED_PNPM)]

    raise AssertionError(
        "pnpm is required on PATH or in the repository recovery runtime"
    )


def test_collection_monitor_frontend_behavior() -> None:
    subprocess.run(
        [
            *_pnpm_command(),
            "--dir",
            "frontend",
            "test",
            "--run",
            *FOCUSED_TESTS,
        ],
        cwd=REPOSITORY,
        check=True,
    )
