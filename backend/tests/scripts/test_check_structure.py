from __future__ import annotations

from pathlib import Path

from research.paths import PROJECT_ROOT
from scripts.check_structure import find_structure_violations


def test_current_repository_structure_has_no_violations() -> None:
    assert find_structure_violations(PROJECT_ROOT) == []


def test_structure_check_detects_root_output_and_reverse_dependency(tmp_path: Path) -> None:
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "research").mkdir(parents=True)
    (tmp_path / "artifacts" / "current").mkdir(parents=True)
    (tmp_path / "result.json").write_text("{}", encoding="utf-8")
    (tmp_path / "backend" / "app" / "bad.py").write_text(
        "from research.paths import PROJECT_ROOT\n",
        encoding="utf-8",
    )
    violations = find_structure_violations(tmp_path)
    assert any("root output file" in item for item in violations)
    assert any("production app cannot import research" in item for item in violations)


def test_structure_check_detects_frontend_and_vague_active_runner(tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend" / "app").mkdir(parents=True)
    research = tmp_path / "backend" / "research" / "validation"
    research.mkdir(parents=True)
    (research / "run_walkforward.py").write_text("", encoding="utf-8")
    violations = find_structure_violations(tmp_path)
    assert any("frontend directory" in item for item in violations)
    assert any("vague name" in item for item in violations)


def test_structure_check_detects_nested_frontend_and_placeholder_runner(tmp_path: Path) -> None:
    (tmp_path / "backend" / "app" / "ui").mkdir(parents=True)
    research = tmp_path / "backend" / "research" / "validation"
    research.mkdir(parents=True)
    (research / "run_v3.py").write_text("", encoding="utf-8")

    violations = find_structure_violations(tmp_path)
    assert any("backend/app/ui/" in item for item in violations)
    assert any("run_v3.py" in item and "vague name" in item for item in violations)


def test_structure_check_reports_unparseable_production_module(tmp_path: Path) -> None:
    app = tmp_path / "backend" / "app"
    app.mkdir(parents=True)
    (tmp_path / "backend" / "research").mkdir()
    (app / "broken.py").write_text("from research.paths import (\n", encoding="utf-8")

    violations = find_structure_violations(tmp_path)
    assert any("cannot inspect production module" in item for item in violations)
