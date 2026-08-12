from pathlib import Path


def test_dependency_layer_does_not_install_project_before_source_copy() -> None:
    project_root = Path(__file__).resolve().parents[2]
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")

    dependency_layer, source_layer = dockerfile.split("COPY backend/app ./app", maxsplit=1)

    assert "--no-install-project" in dependency_layer
    assert source_layer


def test_runtime_command_does_not_resync_project() -> None:
    project_root = Path(__file__).resolve().parents[2]
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")

    assert 'CMD ["uv", "run", "--no-sync", "uvicorn"' in dockerfile
    assert '"--proxy-headers"' in dockerfile
