"""Check repository layout and dependency boundaries without importing the app."""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from research.paths import PROJECT_ROOT

FORBIDDEN_ROOT_OUTPUT_SUFFIXES = frozenset({".csv", ".html", ".json", ".log", ".parquet"})
FORBIDDEN_FRONTEND_DIRS = frozenset({"frontend", "web", "ui"})
ALLOWED_CURRENT_FILES = frozenset({
    "diag_f4_regime.json",
    "mvp_backtest.html",
    "mvp_backtest.json",
    "regime_ensemble_report.html",
    "strategy_regime_ensemble.json",
})
VAGUE_ACTIVE_RUNNERS = frozenset({
    "run_iterate.py",
    "run_opt_grid.py",
    "run_opt_v2.py",
    "run_optimizations.py",
    "run_range_bt.py",
    "run_regime.py",
    "run_verify_period.py",
    "run_walkforward.py",
})
VAGUE_PLACEHOLDER_RUNNER = re.compile(
    r"^run_(?:v\d+|new(?:_v\d+)?|test(?:_v\d+)?|tmp(?:_v\d+)?|temp(?:_v\d+)?|final(?:_v\d+)?)\.py$"
)
LEGACY_COMPAT_RUNNERS = {
    "optimization/run_iterate.py": (
        "research.legacy.optimization.run_structural_bull_return_target_iteration"
    ),
    "optimization/run_opt_grid.py": "research.legacy.optimization.run_strategy_parameter_grid",
    "optimization/run_opt_v2.py": "research.legacy.optimization.run_strategy_deep_grid_walkforward",
    "optimization/run_optimizations.py": (
        "research.legacy.optimization.run_strategy_optimization_baseline"
    ),
    "regime/run_engine_regime.py": "research.legacy.regime.run_leader_index_engine_gate_replay",
    "regime/run_engine_soft.py": "research.legacy.regime.run_leader_index_soft_exposure_replay",
    "regime/run_leader_regime.py": "research.legacy.regime.run_leader_index_regime_replay",
    "regime/run_regime.py": "research.legacy.regime.run_market_breadth_ma120_replay",
    "reporting/regen_leader_report.py": "research.legacy.reporting.regenerate_leader_index_report",
    "validation/run_one_trade_detail.py": "research.legacy.validation.run_pullback_trade_detail",
    "validation/run_range_bt.py": "research.legacy.validation.run_structural_bull_range_replay",
    "validation/run_verify_period.py": (
        "research.legacy.validation.run_structural_bull_trade_attribution"
    ),
    "validation/run_walkforward.py": (
        "research.legacy.validation.run_concentrated_pullback_multiperiod_replay"
    ),
}
FRONTEND_SCAN_ROOTS = ("backend", "scripts", "packaging")
FRONTEND_SCAN_EXCLUDED_DIRS = frozenset({".venv", "__pycache__", "node_modules"})


def _imports_research(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "research" or alias.name.startswith("research.") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "research" or module.startswith("research."):
                return True
    return False


def _is_main_guard(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Eq)
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "__main__"
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Call)
        and isinstance(node.body[0].value.func, ast.Name)
        and node.body[0].value.func.id == "main"
        and not node.orelse
    )


def _is_legacy_compat_runner(path: Path, research_root: Path) -> bool:
    relative = path.relative_to(research_root).as_posix()
    expected_module = LEGACY_COMPAT_RUNNERS.get(relative)
    if expected_module is None:
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return False
    body = list(tree.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return (
        len(body) == 2
        and isinstance(body[0], ast.ImportFrom)
        and body[0].module == expected_module
        and [(alias.name, alias.asname) for alias in body[0].names] == [("main", None)]
        and _is_main_guard(body[1])
    )


def _vague_active_runner(path: Path) -> bool:
    return path.name in VAGUE_ACTIVE_RUNNERS or bool(VAGUE_PLACEHOLDER_RUNNER.fullmatch(path.name))


def _nested_frontend_dirs(root: Path):
    for relative_root in FRONTEND_SCAN_ROOTS:
        scan_root = root / relative_root
        if not scan_root.exists():
            continue
        for current, dirs, _files in os.walk(scan_root):
            dirs[:] = [name for name in dirs if name not in FRONTEND_SCAN_EXCLUDED_DIRS]
            current_path = Path(current)
            for name in dirs:
                if name.lower() in FORBIDDEN_FRONTEND_DIRS:
                    yield current_path / name


def find_structure_violations(project_root: Path = PROJECT_ROOT) -> list[str]:
    root = project_root.resolve()
    violations: list[str] = []

    for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if path.is_file() and path.suffix.lower() in FORBIDDEN_ROOT_OUTPUT_SUFFIXES:
            violations.append(f"root output file must move to artifacts/: {path.name}")
        if path.is_dir() and path.name.lower() in FORBIDDEN_FRONTEND_DIRS:
            violations.append(f"frontend directory is outside project scope: {path.name}/")

    for path in sorted(_nested_frontend_dirs(root)):
        violations.append(
            f"frontend directory is outside project scope: {path.relative_to(root).as_posix()}/"
        )

    backend_root = root / "backend"
    for path in sorted(backend_root.glob("*.py")):
        violations.append(f"Python module must live in app/research/scripts/tests: backend/{path.name}")

    app_root = backend_root / "app"
    for path in sorted(app_root.rglob("*.py")):
        try:
            imports_research = _imports_research(path)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            violations.append(
                f"cannot inspect production module: {path.relative_to(root).as_posix()} ({type(exc).__name__})"
            )
            continue
        if imports_research:
            violations.append(f"production app cannot import research: {path.relative_to(root).as_posix()}")

    research_root = backend_root / "research"
    legacy_root = research_root / "legacy"
    for path in sorted(research_root.rglob("*.py")):
        if legacy_root in path.parents:
            continue
        relative = path.relative_to(research_root).as_posix()
        if relative in LEGACY_COMPAT_RUNNERS:
            if not _is_legacy_compat_runner(path, research_root):
                violations.append(
                    "active research runner has vague name and its legacy compatibility entry "
                    f"must stay a thin shim: {path.relative_to(root).as_posix()}"
                )
            continue
        if _vague_active_runner(path):
            violations.append(f"active research runner has vague name: {path.relative_to(root).as_posix()}")

    current_root = root / "artifacts" / "current"
    if current_root.exists():
        actual = {path.name for path in current_root.iterdir() if path.is_file()}
        for name in sorted(actual - ALLOWED_CURRENT_FILES):
            violations.append(f"unexpected current artifact; archive or whitelist explicitly: {name}")
    return violations


def main() -> int:
    violations = find_structure_violations()
    if violations:
        print("Repository structure check failed:")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print("Repository structure check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
