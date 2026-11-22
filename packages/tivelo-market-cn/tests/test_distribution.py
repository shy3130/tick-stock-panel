import ast
import subprocess
import sys
from pathlib import Path

import tivelo_market_cn


def test_market_package_cannot_import_workbench_or_engine_implementations():
    forbidden = {"app", "fastapi", "tivelo_data", "tivelo_backtest", "tivelo_pro_dynamic"}
    for file in Path(tivelo_market_cn.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                assert node.module.split(".")[0] not in forbidden, file.name
            elif isinstance(node, ast.Import):
                assert not {alias.name.split(".")[0] for alias in node.names} & forbidden, file.name


def test_calendar_import_needs_no_vector_runtime_or_application():
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; import tivelo_market_cn.calendar; "
            "assert not any(n == 'app' or n.startswith('app.') for n in sys.modules); "
            "assert 'polars' not in sys.modules; assert 'numpy' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
