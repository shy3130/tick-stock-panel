from __future__ import annotations

import tomllib
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_hatchling_readme_stays_inside_backend_project() -> None:
    config = tomllib.loads(
        (BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    readme = config["project"]["readme"]

    assert isinstance(readme, str)
    readme_path = (BACKEND_ROOT / readme).resolve()
    assert readme_path.is_relative_to(BACKEND_ROOT.resolve()), (
        "Hatchling rejects project metadata files outside the backend directory"
    )
    assert readme_path.is_file()
