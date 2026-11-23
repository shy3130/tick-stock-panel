"""Check all shipped modules, including optional imports inside functions."""

import ast
from pathlib import Path

import tivelo_backtest


def test_no_workbench_or_concrete_data_market_extension_dependency():
    forbidden = {
        "app",
        "tivelo_data",
        "tivelo_market_cn",
        "tivelo_pro_dynamic",
        "fastapi",
        "duckdb",
        "pyarrow",
    }
    root = Path(tivelo_backtest.__file__).parent
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                imports = [node.module or ""]
            else:
                continue
            assert all(name.split(".")[0] not in forbidden for name in imports), path
