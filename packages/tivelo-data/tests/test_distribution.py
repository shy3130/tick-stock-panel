import ast
import subprocess
import sys
from pathlib import Path

import tivelo_data


def test_package_has_no_dependency_on_workbench_or_backtest_implementation():
    forbidden = {"app", "fastapi", "tivelo_backtest", "tivelo_pro_dynamic"}
    for file in Path(tivelo_data.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                assert node.module.split(".")[0] not in forbidden, file.name
            elif isinstance(node, ast.Import):
                assert not {alias.name.split(".")[0] for alias in node.names} & forbidden, file.name


def test_v1_plugin_import_does_not_initialize_storage_or_application():
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; import tivelo_data.legacy.normalizer; "
            "assert not any(n == 'app' or n.startswith('app.') for n in sys.modules); "
            "assert 'tivelo_data.engine' not in sys.modules; "
            "assert 'tivelo_data.storage' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
