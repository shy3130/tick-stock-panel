from pathlib import Path

from app.spa_entry import spa_entry_path


def test_collection_monitor_uses_isolated_frontend_entry(tmp_path: Path) -> None:
    assert spa_entry_path(tmp_path, "collection-monitor") == (
        tmp_path / "collection-monitor.html"
    )
    assert spa_entry_path(tmp_path, "collection-monitor/") == (
        tmp_path / "collection-monitor.html"
    )


def test_other_spa_routes_keep_the_shared_entry(tmp_path: Path) -> None:
    assert spa_entry_path(tmp_path, "") == tmp_path / "index.html"
    assert spa_entry_path(tmp_path, "dow-monitor") == tmp_path / "index.html"


def test_docker_build_preserves_an_isolated_collection_monitor_entry() -> None:
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    contents = dockerfile.read_text(encoding="utf-8")

    assert "cp ./dist/index.html ./dist/collection-monitor.html" in contents
