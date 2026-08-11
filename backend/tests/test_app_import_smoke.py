from pathlib import Path

from fastapi.testclient import TestClient

from app import main


def test_app_import_and_health_route_smoke() -> None:
    assert main.Path is Path
    dirs = main._strategy_dirs(Path("data"))
    assert dirs[0] == Path(main.__file__).resolve().parent / "strategy" / "builtin"
    assert dirs[0].is_dir()

    response = TestClient(main.app).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
