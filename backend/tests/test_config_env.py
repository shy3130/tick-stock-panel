from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, project_root_from_config_file


def test_settings_reads_server_and_auth_values_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in ("HOST", "PORT", "LOG_LEVEL", "AUTH_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "HOST=127.0.0.1\n"
        "PORT=4318\n"
        "LOG_LEVEL=DEBUG\n"
        "AUTH_PASSWORD=config-secret\n",
        encoding="utf-8",
    )

    configured = Settings(_env_file=env_path)

    assert configured.host == "127.0.0.1"
    assert configured.port == 4318
    assert configured.log_level == "DEBUG"
    assert configured.auth_password == "config-secret"


def test_docker_flat_layout_resolves_project_root_to_app_dir() -> None:
    """镜像把 backend/app 拷到 /app/app。再向上三级会落到 /，相对 ./data 变成 /data。"""
    assert project_root_from_config_file(Path("/app/app/config.py")) == Path("/app")


def test_dev_layout_project_root_stays_repository_root(tmp_path: Path) -> None:
    config_file = tmp_path / "repo" / "backend" / "app" / "config.py"
    assert project_root_from_config_file(config_file) == tmp_path / "repo"


def test_relative_data_dir_resolves_against_project_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """DATA_DIR=./data 必须落在应用根的 data/，不能落到文件系统根的 /data。"""
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setattr("app.config._PROJECT_ROOT", tmp_path)
    configured = Settings(_env_file=None, data_dir=Path("./data"))
    assert configured.data_dir == (tmp_path / "data").resolve()
