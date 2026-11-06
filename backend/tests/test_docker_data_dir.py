from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app import config


def mount_line(point: Path | str, filesystem: str = "ext4") -> str:
    escaped = str(point).replace("\\", r"\134").replace(" ", r"\040")
    return f"2 1 8:1 / {escaped} rw shared:1 - {filesystem} /dev/test rw\n"


@pytest.fixture
def docker_layout(tmp_path, monkeypatch):
    root = tmp_path / "container"
    app = root / "app"
    (app / "app").mkdir(parents=True)
    mounts = tmp_path / "mountinfo"
    mounts.write_text(mount_line("/", "overlay") + mount_line(app / "data"))
    monkeypatch.setattr(config, "__file__", str(app / "app" / "config.py"))
    monkeypatch.setattr(config, "_PROJECT_ROOT", root)
    monkeypatch.setattr(config, "_IS_FROZEN", False)
    monkeypatch.setattr(config, "_DOCKER_MOUNTINFO", mounts, raising=False)
    monkeypatch.setenv("TSP_DOCKER", "1")
    monkeypatch.delenv("DATA_DIR", raising=False)
    return root, app, mounts


@pytest.mark.parametrize("value", ["./data", "data", "./data/"])
def test_docker_env_file_uses_mounted_app_data(docker_layout, tmp_path, value, caplog):
    root, app, _ = docker_layout
    env = tmp_path / "example.env"
    env.write_text(f"DATA_DIR={value}\n")
    with caplog.at_level(logging.WARNING, logger="app.config"):
        configured = config.Settings(_env_file=env)
    assert configured.data_dir == app / "data"
    assert not (root / "data").exists()
    assert not (app / "data").exists()  # Resolution never creates or migrates data.
    assert not caplog.records
    assert config._user_data_root() == app / "data"


def test_docker_custom_relative_directory_uses_app_root(docker_layout):
    _, app, mounts = docker_layout
    mounts.write_text(mount_line(app / "custom"))
    assert config.Settings(_env_file=None, data_dir="custom").data_dir == app / "custom"


def test_absolute_custom_directory_is_preserved(docker_layout, tmp_path, caplog):
    root, _, mounts = docker_layout
    legacy = root / "data"
    legacy.mkdir()
    (legacy / "keep").write_bytes(b"legacy-data")
    custom = tmp_path / "external volume" / "nested"
    mounts.write_text(mount_line(custom.parent))
    configured = config.Settings(_env_file=None, data_dir=custom)
    assert configured.data_dir == custom
    assert not caplog.records
    assert not custom.exists()
    assert (legacy / "keep").read_bytes() == b"legacy-data"


@pytest.mark.parametrize("docker,frozen", [("0", False), ("", False), ("1", True)])
def test_source_and_desktop_relative_paths_are_unchanged(
    docker_layout, monkeypatch, docker, frozen, caplog,
):
    root, _, _ = docker_layout
    monkeypatch.setenv("TSP_DOCKER", docker)
    monkeypatch.setattr(config, "_IS_FROZEN", frozen)
    assert config.Settings(_env_file=None, data_dir="./data").data_dir == root / "data"
    assert not caplog.records


def test_existing_mounted_legacy_directory_is_kept(docker_layout, caplog):
    root, app, mounts = docker_layout
    legacy = root / "data"
    legacy.mkdir()
    saved = legacy / "secrets-sentinel"
    saved.write_bytes(b"keep-old-data")
    mounts.write_text(mount_line(legacy) + mount_line(app / "data"))
    configured = config.Settings(_env_file=None, data_dir="./data")
    assert configured.data_dir == legacy
    assert saved.read_bytes() == b"keep-old-data"
    assert "沿用" in caplog.text


def test_unmounted_legacy_data_blocks_silent_switch_and_never_overwrites(
    docker_layout, monkeypatch,
):
    root, app, _ = docker_layout
    legacy = root / "data"
    target = app / "data"
    legacy.mkdir()
    target.mkdir()
    old = legacy / "old.json"
    new = target / "new.json"
    old.write_bytes(b"old-credentials")
    new.write_bytes(b"new-credentials")
    monkeypatch.setenv("AI_API_KEY", "audit-secret-must-not-appear")
    with pytest.raises(RuntimeError, match="先备份") as failure:
        config.Settings(_env_file=None, data_dir="./data")
    assert "audit-secret-must-not-appear" not in str(failure.value)
    assert old.read_bytes() == b"old-credentials"
    assert new.read_bytes() == b"new-credentials"
    assert sorted(p.name for p in legacy.iterdir()) == ["old.json"]
    assert sorted(p.name for p in target.iterdir()) == ["new.json"]


def test_unreadable_legacy_directory_does_not_switch(docker_layout, monkeypatch):
    root, _, _ = docker_layout
    legacy = root / "data"
    legacy.mkdir()
    original = Path.iterdir

    def iterdir(path):
        if path == legacy:
            raise PermissionError("denied")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    with pytest.raises(RuntimeError, match="无法检查"):
        config.Settings(_env_file=None, data_dir="./data")


@pytest.mark.parametrize("mounts_kind", [
    "missing", "unreadable", "malformed", "root", "sibling", "tmpfs", "nested_tmpfs",
])
def test_mount_check_warns_without_breaking_startup(
    docker_layout, monkeypatch, mounts_kind, caplog,
):
    _, app, mounts = docker_layout
    if mounts_kind == "missing":
        mounts.unlink()
    elif mounts_kind == "unreadable":
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == mounts:
                raise PermissionError("denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
    elif mounts_kind == "malformed":
        mounts.write_text("bad mountinfo\n")
    elif mounts_kind == "root":
        mounts.write_text(mount_line("/", "overlay"))
    elif mounts_kind == "sibling":
        mounts.write_text(mount_line(app / "data-other"))
    elif mounts_kind == "nested_tmpfs":
        mounts.write_text(mount_line(app) + mount_line(app / "data", "tmpfs"))
    else:
        mounts.write_text(mount_line(app / "data", "tmpfs"))
    assert config.Settings(_env_file=None, data_dir="./data").data_dir == app / "data"
    assert "持久化挂载" in caplog.text


def test_missing_mount_info_does_not_hide_unmigrated_data(docker_layout):
    root, _, mounts = docker_layout
    legacy = root / "data"
    legacy.mkdir()
    (legacy / "keep").write_bytes(b"keep")
    mounts.unlink()
    with pytest.raises(RuntimeError, match="先备份"):
        config.Settings(_env_file=None, data_dir="./data")
